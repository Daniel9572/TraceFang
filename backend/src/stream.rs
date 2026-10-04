use std::{collections::BTreeMap,sync::Mutex};
use chrono::Utc;
use serde_json::{Value,json};
use tokio::sync::broadcast;

pub type StreamKey=(String,String,String); // source, canonical instrument, period
struct Pump {sender:broadcast::Sender<Value>,sequence:u64}
#[derive(Default)]
pub struct StreamHub {pumps:Mutex<BTreeMap<StreamKey,Pump>>}

impl StreamHub {
    pub fn subscribe(&self,source:&str,symbol:&str,period:&str)->broadcast::Receiver<Value> {
        let mut pumps=self.pumps.lock().expect("stream lock");
        let pump=pumps.entry((source.into(),symbol.into(),period.into())).or_insert_with(||{
            let (sender,_)=broadcast::channel(256);Pump{sender,sequence:0}
        });
        pump.sender.subscribe()
    }
    pub fn active(&self,source:&str,symbol:&str)->Vec<String> {
        let mut pumps=self.pumps.lock().expect("stream lock");
        pumps.retain(|_,p|p.sender.receiver_count()>0);
        pumps.keys().filter(|(s,i,_)|s==source&&i==symbol).map(|(_,_,p)|p.clone()).collect()
    }
    pub fn publish(&self,source:&str,symbol:&str,period:Option<&str>,event:Value) {
        let mut pumps=self.pumps.lock().expect("stream lock");
        for ((s,i,p),pump) in pumps.iter_mut() {
            if s!=source||i!=symbol||period.is_some_and(|v|v!=p) {continue}
            let Some(sequence)=pump.sequence.checked_add(1) else {tracing::error!("stream delivery sequence exhausted");continue;};pump.sequence=sequence;
            let mut value=event.clone();
            value["delivery_sequence"]=json!(pump.sequence.to_string());
            value["period_id"]=json!(p);
            value["emitted_at"]=json!(Utc::now());
            let _=pump.sender.send(value);
        }
    }
    pub fn status(&self,source:&str,symbol:&str,error:&str) {
        self.publish(source,symbol,None,json!({"kind":"status","state":"unavailable","error":error}));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn sequences_are_per_dataset_and_slow_receivers_report_loss() {
        let hub=StreamHub::default();
        let mut a=hub.subscribe("one","XAU/USD","1m");
        let mut b=hub.subscribe("two","XAU/USD","1m");
        hub.publish("one","XAU/USD",None,json!({"kind":"quote"}));
        assert_eq!(a.recv().await.unwrap()["delivery_sequence"],"1");
        assert!(matches!(b.try_recv(),Err(broadcast::error::TryRecvError::Empty)));
        for _ in 0..300 {hub.publish("one","XAU/USD",None,json!({"kind":"quote"}));}
        assert!(matches!(a.recv().await,Err(broadcast::error::RecvError::Lagged(_))));
        assert!(a.recv().await.unwrap()["delivery_sequence"].as_str().unwrap().parse::<u64>().unwrap()>2);
    }

    #[tokio::test]
    async fn u64_delivery_boundary_is_exact_and_never_wraps() {
        let hub=StreamHub::default();let mut receiver=hub.subscribe("one","XAU/USD","1m");
        hub.pumps.lock().unwrap().get_mut(&("one".into(),"XAU/USD".into(),"1m".into())).unwrap().sequence=u64::MAX-1;
        hub.publish("one","XAU/USD",None,json!({"kind":"quote"}));
        assert_eq!(receiver.recv().await.unwrap()["delivery_sequence"],u64::MAX.to_string());
        hub.publish("one","XAU/USD",None,json!({"kind":"quote"}));
        assert!(matches!(receiver.try_recv(),Err(broadcast::error::TryRecvError::Empty)));
        assert_eq!(hub.pumps.lock().unwrap().values().next().unwrap().sequence,u64::MAX);
    }
}
