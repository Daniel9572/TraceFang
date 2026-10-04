//! Confirmed-right-side OHLC patterns. These never infer trader identity.
use super::exact::n;
use super::{exact::{D,d,div,text,decimal},quant::{QuantBar,PatternParameters}};
use chrono::{DateTime,Utc};
use num_traits::Zero;
use serde::Serialize;
use std::collections::BTreeMap;

pub fn atr(bars:&[QuantBar],index:usize,period:usize,partial:bool)->Option<D>{
    if !partial && index+1<period{return None;}
    let start=(index+1).saturating_sub(period);let mut sum=D::zero();
    for i in start..=index{let bar=&bars[i];let mut range=&bar.high-&bar.low;
        if i>0{range=range.max((&bar.high-&bars[i-1].close).abs()).max((&bar.low-&bars[i-1].close).abs());}sum+=range;}
    Some(div(&sum,&n(index-start+1)))
}
fn cached_atr(bars:&[QuantBar],period:usize)->Vec<D>{let mut rolling=D::zero();let mut ranges:Vec<D>=vec![];let mut out=vec![];for(i,b)in bars.iter().enumerate(){let mut range=&b.high-&b.low;if i>0{range=range.max((&b.high-&bars[i-1].close).abs()).max((&b.low-&bars[i-1].close).abs());}rolling+=&range;ranges.push(range);if i>=period{rolling-=&ranges[i-period];}out.push(div(&rolling,&n((i+1).min(period))));}out}
#[derive(Clone,Debug,Serialize,serde::Deserialize)]
pub struct Anchor{pub index:usize,pub time:DateTime<Utc>,#[serde(with="decimal")]pub price:D}
#[derive(Clone,Debug,Serialize)]
pub struct Pivot{pub kind:&'static str,#[serde(flatten)]pub anchor:Anchor,pub confirmed_at_index:usize}
fn anchor(bars:&[QuantBar],index:usize,price:D)->Anchor{Anchor{index,time:bars[index].open_time,price}}
pub fn pivots(bars:&[QuantBar],end:usize,radius:usize,lookback:usize,mode:&str)->Vec<Pivot>{
    let mut result=Vec::new();if end<radius*2{return result;}
    let start=radius.max((end+1).saturating_sub(lookback));
    for i in start..=end-radius{let bar=&bars[i];let(mut high,mut low,mut strict_high,mut strict_low)=(true,true,false,false);
        for j in i-radius..=i+radius{if i==j{continue;}let neighbor=&bars[j];
            if mode=="pattern"{if j<i{high&=bar.high>neighbor.high;low&=bar.low<neighbor.low;}else{high&=bar.high>=neighbor.high;low&=bar.low<=neighbor.low;}}
            else if mode=="trend"{high&=bar.high>neighbor.high;low&=bar.low<neighbor.low;}
            else{high&=bar.high>=neighbor.high;low&=bar.low<=neighbor.low;strict_high|=bar.high>neighbor.high;strict_low|=bar.low<neighbor.low;}}
        if high&&(mode!="smart"||strict_high){result.push(Pivot{kind:"high",anchor:anchor(bars,i,bar.high.clone()),confirmed_at_index:i+radius});}
        if low&&(mode!="smart"||strict_low){result.push(Pivot{kind:"low",anchor:anchor(bars,i,bar.low.clone()),confirmed_at_index:i+radius});}}
    result.sort_by(|a,b|a.anchor.index.cmp(&b.anchor.index).then(a.kind.cmp(b.kind)));result
}
#[derive(Clone,Debug,Serialize)]
pub struct Pattern{pub id:String,pub kind:&'static str,pub label:&'static str,pub direction:&'static str,pub status:&'static str,
    pub first:Anchor,pub neckline:Option<Anchor>,pub second:Anchor,pub confirmation:Anchor,
    #[serde(with="decimal")]pub trigger_price:D,#[serde(with="decimal")]pub invalidation_price:D,
    pub detected_at:DateTime<Utc>,pub invalidated_at:Option<DateTime<Utc>>,#[serde(with="decimal")]pub confidence:D,pub evidence:Vec<String>}
fn invalidates(bar:&QuantBar,bullish:bool,price:&D)->bool{if bullish{bar.close<*price}else{bar.close>*price}}
#[allow(clippy::too_many_arguments)]
fn pattern(bars:&[QuantBar],end:usize,kind:&'static str,first:Anchor,neckline:Option<Anchor>,second:Anchor,confirmation:usize,trigger:D,invalidation:D,confidence:D,evidence:Vec<String>)->Pattern{
    let bullish=["double-bottom","two-b-bottom"].contains(&kind);
    let invalidated=(confirmation+1..=end).find(|&i|invalidates(&bars[i],bullish,&invalidation));
    Pattern{id:format!("{kind}:{}:{}:{}",first.time,second.time,bars[confirmation].open_time),kind,
        label:match kind{"double-bottom"=>"W 底颈线突破","double-top"=>"M 顶颈线跌破","two-b-bottom"=>"2B 底部假突破",_=>"2B 顶部假突破"},
        direction:if bullish{"bullish"}else{"bearish"},status:if invalidated.is_some(){"invalidated"}else{"confirmed"},first,neckline,second,
        confirmation:anchor(bars,confirmation,bars[confirmation].close.clone()),trigger_price:trigger,invalidation_price:invalidation,
        detected_at:bars[confirmation].open_time,invalidated_at:invalidated.map(|i|bars[i].open_time),confidence,evidence}
}
#[derive(Clone,Debug,Serialize)]
pub struct Structure{#[serde(with="decimal")]pub support:D,#[serde(with="decimal")]pub resistance:D,pub swings:Vec<Pivot>,pub patterns:Vec<Pattern>}
pub fn price_structure(bars:&[QuantBar],end:usize,p:&PatternParameters)->Structure{price_structure_cached(bars,end,p,&cached_atr(bars,14))}
pub fn price_structure_cached(bars:&[QuantBar],end:usize,p:&PatternParameters,cache:&[D])->Structure{
    let swings=pivots(bars,end,p.pivot_radius,p.lookback_bars,"pattern");let mut found=BTreeMap::new();
    for kind in ["low","high"]{let same:Vec<_>=swings.iter().filter(|v|v.kind==kind).collect();let bull=kind=="low";
        for pair in same.windows(2){let(first,second)=(pair[0],pair[1]);let separation=second.anchor.index-first.anchor.index;
            if separation<p.double_minimum_separation||separation>p.double_maximum_separation{continue;}
            let avg=div(&(&first.anchor.price+&second.anchor.price),&d("2"));let volatility=cache[second.anchor.index].clone().max(bars[second.anchor.index].close.abs()*d("0.00000001"));
            let tolerance=(&avg.abs()*&p.double_price_tolerance_percent).max(&volatility*&p.double_price_tolerance_atr);
            if (&first.anchor.price-&second.anchor.price).abs()>tolerance{continue;}
            let mut trigger_index=first.anchor.index;let mut trigger=if bull{bars[trigger_index].high.clone()}else{bars[trigger_index].low.clone()};
            for i in first.anchor.index..=second.anchor.index{let value=if bull{&bars[i].high}else{&bars[i].low};if (bull&&value>&trigger)||(!bull&&value<&trigger){trigger=value.clone();trigger_index=i;}}
            let height=if bull{&trigger-&avg}else{&avg-&trigger};if height<&volatility*&p.minimum_pattern_height_atr{continue;}
            let buffer=&volatility*&p.confirmation_buffer_atr;
            let invalidation=if bull{first.anchor.price.clone().min(second.anchor.price.clone())-&volatility*&p.invalidation_buffer_atr}else{first.anchor.price.clone().max(second.anchor.price.clone())+&volatility*&p.invalidation_buffer_atr};
            let mut confirmation=None;
            for i in second.confirmed_at_index..=end.min(second.confirmed_at_index+p.double_confirmation_bars){if invalidates(&bars[i],bull,&invalidation){break;}
                if (bull&&bars[i].close>&trigger+&buffer)||(!bull&&bars[i].close<&trigger-&buffer){confirmation=Some(i);break;}}
            if let Some(i)=confirmation{let v=pattern(bars,end,if bull{"double-bottom"}else{"double-top"},first.anchor.clone(),Some(anchor(bars,trigger_index,trigger.clone())),second.anchor.clone(),i,trigger,invalidation,d("0.68"),vec![format!("双极值间隔 {separation} Bar；右侧 {} 根确认",p.pivot_radius)]);found.insert(v.id.clone(),v);}}
    }
    for prior in &swings{let bull=prior.kind=="low";
        for probe in prior.confirmed_at_index+1..=end.min(prior.confirmed_at_index+p.two_b_maximum_bars){let volatility=cache[probe].clone().max(bars[probe].close.abs()*d("0.00000001"));
            if volatility.is_zero(){continue;}
            let breach=if bull{&prior.anchor.price-&bars[probe].low}else{&bars[probe].high-&prior.anchor.price};
            if breach<&volatility*&p.two_b_minimum_breach_atr||breach>&volatility*&p.two_b_maximum_breach_atr{continue;}
            let probe_price=if bull{bars[probe].low.clone()}else{bars[probe].high.clone()};let buffer=&volatility*&p.confirmation_buffer_atr;
            let invalidation=if bull{&probe_price-&volatility*&p.invalidation_buffer_atr}else{&probe_price+&volatility*&p.invalidation_buffer_atr};
            let mut confirmation=None;for i in probe..=end.min(probe+p.two_b_confirmation_bars){if invalidates(&bars[i],bull,&invalidation){break;}
                if (bull&&bars[i].close>&prior.anchor.price+&buffer)||(!bull&&bars[i].close<&prior.anchor.price-&buffer){confirmation=Some(i);break;}}
            if let Some(i)=confirmation{let v=pattern(bars,end,if bull{"two-b-bottom"}else{"two-b-top"},prior.anchor.clone(),None,anchor(bars,probe,probe_price),i,prior.anchor.price.clone(),invalidation,d("0.64"),vec![format!("极值越界 {} ATR，{} Bar 内收回",text(&div(&breach,&volatility)),i-probe)]);found.insert(v.id.clone(),v);break;}}
    }
    let start=(end+1).saturating_sub(p.lookback_bars);
    let support=swings.iter().rev().find(|v|v.kind=="low"&&v.anchor.price<bars[end].close).map(|v|v.anchor.price.clone()).unwrap_or_else(||bars[start..=end].iter().map(|v|v.low.clone()).min().unwrap());
    let resistance=swings.iter().rev().find(|v|v.kind=="high"&&v.anchor.price>bars[end].close).map(|v|v.anchor.price.clone()).unwrap_or_else(||bars[start..=end].iter().map(|v|v.high.clone()).max().unwrap());
    let mut patterns:Vec<_>=found.into_values().collect();patterns.sort_by(|a,b|a.detected_at.cmp(&b.detected_at).then(a.id.cmp(&b.id)));if patterns.len()>12{patterns.drain(..patterns.len()-12);}
    Structure{support,resistance,swings,patterns}
}

#[derive(Clone,Debug,Serialize)]
pub struct StructureEvent{pub id:String,pub kind:String,pub label:&'static str,pub direction:&'static str,pub status:&'static str,
    pub reference:Anchor,pub confirmation:Anchor,pub detected_at:DateTime<Utc>,pub invalidated_at:Option<DateTime<Utc>>,#[serde(with="decimal")]pub confidence:D,pub evidence:Vec<String>}
pub fn smart_events(bars:&[QuantBar],end:usize)->Vec<StructureEvent>{smart_events_cached(bars,end,&cached_atr(bars,14))}
pub fn smart_events_cached(bars:&[QuantBar],end:usize,cache:&[D])->Vec<StructureEvent>{
    if end<18{return vec![];}let pivots=pivots(bars,end,2,221,"smart");let mut events=vec![];
    for i in 14.max(end.saturating_sub(220))..=end{let available:Vec<_>=pivots.iter().filter(|v|v.confirmed_at_index<=i&&v.anchor.index<i).collect();
        let high:Vec<_>=available.iter().filter(|v|v.kind=="high").collect();let low:Vec<_>=available.iter().filter(|v|v.kind=="low").collect();
        let trend=if high.len()>=2&&low.len()>=2{let(h,l)=(high.len(),low.len());if high[h-1].anchor.price>high[h-2].anchor.price&&low[l-1].anchor.price>low[l-2].anchor.price{"bullish"}else if high[h-1].anchor.price<high[h-2].anchor.price&&low[l-1].anchor.price<low[l-2].anchor.price{"bearish"}else{"mixed"}}else{"mixed"};
        let volatility=cache[i].clone();if volatility.is_zero(){continue;}
        for (list,bull) in [(high,false),(low,true)]{let Some(prior)=list.last()else{continue;};let overshoot=if bull{div(&(&prior.anchor.price-&bars[i].low),&volatility)}else{div(&(&bars[i].high-&prior.anchor.price),&volatility)};
            let reclaim=if bull{bars[i].close>&prior.anchor.price+&volatility*d("0.02")}else{bars[i].close<&prior.anchor.price-&volatility*d("0.02")};
            let mut candidates=vec![];if overshoot>=d("0.08")&&overshoot<=d("1.5")&&reclaim{candidates.push((if bull{"low-liquidity-sweep"}else{"high-liquidity-sweep"}.to_owned(),if bull{"bullish"}else{"bearish"},d("0.56")+(&overshoot*d("0.12")).min(d("0.18"))));}
            let threshold=if bull{&prior.anchor.price-&volatility*d("0.12")}else{&prior.anchor.price+&volatility*d("0.12")};
            let breaks=if bull{bars[i-1].close>=threshold&&bars[i].close<threshold}else{bars[i-1].close<=threshold&&bars[i].close>threshold};
            if breaks{let direction=if bull{"bearish"}else{"bullish"};let choch=trend==if bull{"bullish"}else{"bearish"};candidates.push((format!("{direction}-{}",if choch{"choch"}else{"bos"}),direction,if trend=="mixed"{d("0.58")}else{d("0.66")}));}
            for(kind,direction,confidence)in candidates{let bullish=direction=="bullish";let invalidated=(i+1..=end).find(|&j|{let a=cache[j].clone();if bullish{bars[j].close<&prior.anchor.price-&a*d("0.5")}else{bars[j].close>&prior.anchor.price+&a*d("0.5")}});
                events.push(StructureEvent{id:format!("sm:{kind}:{}:{}",prior.anchor.time,bars[i].open_time),label:if kind.contains("sweep"){"SWEEP"}else if kind.contains("choch"){"CHOCH"}else{"BOS"},kind,direction,status:if invalidated.is_some(){"invalidated"}else{"confirmed"},reference:prior.anchor.clone(),confirmation:anchor(bars,i,bars[i].close.clone()),detected_at:bars[i].open_time,invalidated_at:invalidated.map(|j|bars[j].open_time),confidence,evidence:vec!["仅为已确认摆动与 OHLC 代理，不识别机构或订单身份".into()]});}}
    }events
}

#[derive(Clone,Debug,Serialize)]
pub struct TrendLine{pub id:String,pub direction:&'static str,pub start:Anchor,pub anchor:Anchor,pub end:Anchor,pub status:&'static str,pub touch_count:usize,#[serde(with="decimal")]pub quality:D,#[serde(with="decimal")]pub atr_error:D,pub invalidated_at:Option<DateTime<Utc>>,pub invalidation_reason:Option<&'static str>,#[serde(skip)]invalidated_index:Option<usize>}
pub fn trend_lines(bars:&[QuantBar],end:usize)->Vec<TrendLine>{trend_lines_cached(bars,end,&cached_atr(bars,14))}
pub fn trend_lines_cached(bars:&[QuantBar],end:usize,cache:&[D])->Vec<TrendLine>{
    if end<12{return vec![];}let mut selected=vec![];
    for(direction,kind)in[("support","low"),("resistance","high")]{let all=pivots(bars,end,2,261,"trend");let same:Vec<_>=all.iter().filter(|v|v.kind==kind).collect();let same=&same[same.len().saturating_sub(10)..];let mut candidates=vec![];
        for right in 1..same.len(){for left in right.saturating_sub(5)..right{let(first,second)=(&same[left].anchor,&same[right].anchor);let span=second.index-first.index;if span<5{continue;}let anchor_atr=if second.index>=13{cache[second.index].clone()}else{&bars[second.index].close*d(".005")};if anchor_atr<=D::zero(){continue;}
            if(direction=="support"&&second.price<&first.price-&anchor_atr*d(".4"))||(direction=="resistance"&&second.price>&first.price+&anchor_atr*d(".4")){continue;}let slope=div(&(&second.price-&first.price),&n(span));if slope.abs()>&anchor_atr*d(".35"){continue;}
            let line_at=|index:usize|&first.price+&slope*n(index-first.index);let(mut touches,mut samples,mut breaches)=(2_usize,0_usize,0_usize);let mut error=D::zero();let mut invalidated=None;let mut reason=None;
            for index in second.index+1..=end{let expected=line_at(index);let volatility=if index>=13{cache[index].clone()}else{anchor_atr.clone()};let tolerance=&volatility*d(".28");let observed=if direction=="support"{&bars[index].low}else{&bars[index].high};let distance=(observed-&expected).abs();error+=div(&distance,&volatility.clone().max(d("0.0000000000000002220446049250313"))).min(d("2"));samples+=1;
                let correct=if direction=="support"{bars[index].close>=&expected-&tolerance}else{bars[index].close<=&expected+&tolerance};if distance<=tolerance&&correct{touches+=1;}let breach=if direction=="support"{&expected-&bars[index].close}else{&bars[index].close-&expected};if breach>tolerance{breaches+=1;}else{breaches=0;}if invalidated.is_none()&&(breaches>=2||breach>&volatility*d(".75")){invalidated=Some(index);reason=Some(if breaches>=2{"连续两根收盘越过趋势线及 ATR 缓冲"}else{"单根收盘突破超过 0.75 ATR"});}}
            let error=if samples==0{D::zero()}else{div(&error,&n(samples))};let duration=div(&n(end-first.index),&d("120")).min(D::from(1));let touch=div(&n(touches-2),&d("4")).min(D::from(1));let quality=(duration*d(".25")+touch*d(".5")+(d("1")-div(&error,&d("1.5")).min(d("1")))*d(".25")).max(D::zero()).min(d("1"));let status=if invalidated.is_some(){"invalidated"}else if touches>=3{"tested"}else if quality>=d(".48"){"confirmed"}else{"candidate"};let endpoint=line_at(end);if endpoint<=D::zero(){continue;}
            candidates.push(TrendLine{id:format!("smart-trend:{direction}:{}:{}",first.time,second.time),direction,start:first.clone(),anchor:second.clone(),end:anchor(bars,end,endpoint),status,touch_count:touches,quality,atr_error:error,invalidated_at:invalidated.map(|index|bars[index].open_time),invalidation_reason:reason,invalidated_index:invalidated});}}
        let active=candidates.iter().filter(|v|v.invalidated_index.is_none()).max_by(|a,b|a.quality.cmp(&b.quality).then(a.anchor.index.cmp(&b.anchor.index))).cloned();let invalidated=candidates.iter().filter(|v|v.invalidated_index.is_some()).max_by_key(|v|v.invalidated_index).cloned();if let Some(v)=active{selected.push(v);}if let Some(v)=invalidated{if end-v.invalidated_index.unwrap()<=80{selected.push(v);}}}
    selected
}

#[derive(Clone,Debug,Serialize,serde::Deserialize,Default)]
pub struct TrendState{candidates:BTreeMap<String,TrendAccumulator>}
#[derive(Clone,Debug,Serialize,serde::Deserialize)]
struct TrendAccumulator{first:Anchor,second:Anchor,#[serde(with="decimal")]slope:D,#[serde(with="decimal")]anchor_atr:D,touches:usize,#[serde(with="decimal")]error:D,samples:usize,breaches:usize,processed:usize,invalidated:Option<usize>,invalidated_at:Option<DateTime<Utc>>,reason:Option<String>}
impl TrendState{
    /// Same candidate rules as trend_lines_cached; historical errors/touches are
    /// accumulated once, with candidate membership rebuilt from confirmed pivots.
    pub fn advance(&mut self,bars:&[QuantBar],end:usize,cache:&[D],offset:usize){self.push_mode(bars,end,cache,offset,false);}
    pub fn push(&mut self,bars:&[QuantBar],end:usize,cache:&[D],offset:usize)->Vec<TrendLine>{self.push_mode(bars,end,cache,offset,true)}
    fn push_mode(&mut self,bars:&[QuantBar],end:usize,cache:&[D],offset:usize,render:bool)->Vec<TrendLine>{if end<12{return vec![];}let global_end=end+offset;let all=pivots(bars,end,2,261,"trend");let mut allowed=std::collections::BTreeSet::new();let mut results=vec![];
        for(direction,kind)in[("support","low"),("resistance","high")]{let same:Vec<_>=all.iter().filter(|p|p.kind==kind).collect();let same=&same[same.len().saturating_sub(10)..];for right in 1..same.len(){for left in right.saturating_sub(5)..right{let(first,second)=(&same[left].anchor,&same[right].anchor);let id=format!("smart-trend:{direction}:{}:{}",first.time,second.time);allowed.insert(id.clone());if !self.candidates.contains_key(&id){let span=second.index-first.index;if span<5{continue;}let anchor_atr=if second.index+offset>=13{cache[second.index].clone()}else{&bars[second.index].close*d(".005")};if anchor_atr<=D::zero(){continue;}if(direction=="support"&&second.price<&first.price-&anchor_atr*d(".4"))||(direction=="resistance"&&second.price>&first.price+&anchor_atr*d(".4")){continue;}let slope=div(&(&second.price-&first.price),&n(span));if slope.abs()>&anchor_atr*d(".35"){continue;}let mut first=first.clone();first.index+=offset;let mut second=second.clone();second.index+=offset;self.candidates.insert(id.clone(),TrendAccumulator{processed:second.index,first,second,slope,anchor_atr,touches:2,error:D::zero(),samples:0,breaches:0,invalidated:None,invalidated_at:None,reason:None});}
            let state=self.candidates.get_mut(&id).unwrap();for global in state.processed+1..=global_end{let index=global-offset;let expected=&state.first.price+&state.slope*n(global-state.first.index);let volatility=if global>=13{cache[index].clone()}else{state.anchor_atr.clone()};let tolerance=&volatility*d(".28");let observed=if direction=="support"{&bars[index].low}else{&bars[index].high};let distance=(observed-&expected).abs();state.error+=div(&distance,&volatility.clone().max(d("0.0000000000000002220446049250313"))).min(d("2"));state.samples+=1;let correct=if direction=="support"{bars[index].close>=&expected-&tolerance}else{bars[index].close<=&expected+&tolerance};if distance<=tolerance&&correct{state.touches+=1;}let breach=if direction=="support"{&expected-&bars[index].close}else{&bars[index].close-&expected};if breach>tolerance{state.breaches+=1;}else{state.breaches=0;}if state.invalidated.is_none()&&(state.breaches>=2||breach>&volatility*d(".75")){state.invalidated=Some(global);state.invalidated_at=Some(bars[index].open_time);state.reason=Some(if state.breaches>=2{"连续两根收盘越过趋势线及 ATR 缓冲"}else{"单根收盘突破超过 0.75 ATR"}.into());}}state.processed=global_end;if !render{continue;}
            let error=if state.samples==0{D::zero()}else{div(&state.error,&n(state.samples))};let duration=div(&n(global_end-state.first.index),&d("120")).min(d("1"));let touch=div(&n(state.touches-2),&d("4")).min(d("1"));let quality=(duration*d(".25")+touch*d(".5")+(d("1")-div(&error,&d("1.5")).min(d("1")))*d(".25")).max(D::zero()).min(d("1"));let status=if state.invalidated.is_some(){"invalidated"}else if state.touches>=3{"tested"}else if quality>=d(".48"){"confirmed"}else{"candidate"};let endpoint=&state.first.price+&state.slope*n(global_end-state.first.index);if endpoint<=D::zero(){continue;}let mut first=state.first.clone();first.index-=offset;let mut second=state.second.clone();second.index-=offset;results.push(TrendLine{id,direction,start:first,anchor:second,end:anchor(bars,end,endpoint),status,touch_count:state.touches,quality,atr_error:error,invalidated_at:state.invalidated_at,invalidation_reason:state.reason.as_ref().map(|s|if s.starts_with("连续"){"连续两根收盘越过趋势线及 ATR 缓冲"}else{"单根收盘突破超过 0.75 ATR"}),invalidated_index:state.invalidated});}}
        }
        self.candidates.retain(|key,_|allowed.contains(key));let mut selected=vec![];for direction in["support","resistance"]{if let Some(line)=results.iter().filter(|v|v.direction==direction&&v.invalidated_at.is_none()).max_by(|a,b|a.quality.cmp(&b.quality).then(a.anchor.index.cmp(&b.anchor.index))).cloned(){selected.push(line);}if let Some(line)=results.iter().filter(|v|v.direction==direction&&v.invalidated_at.is_some()).max_by_key(|v|v.invalidated_at).cloned(){if global_end-line.invalidated_index.unwrap()<=80{selected.push(line);}}}selected
    }
}
