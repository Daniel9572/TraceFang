//! Causal next-open price-series simulation with an exact signed cash ledger.
use super::{exact::{D,d,div,round,text,decimal,optional_decimal,sqrt},quant::{QuantBar,IndicatorPoint,Parameters,HistorySemantics,content_hash,CALCULATION_VERSION}};
use anyhow::{Result,ensure};
use chrono::{DateTime,Utc,NaiveDate};
use num_traits::{Zero,One,Signed};
use serde::{Serialize,Deserialize};
use serde_json::Value;

pub const EXECUTION_VERSION:&str="tracefang-next-open-signed-ledger-v1";
#[derive(Clone,Debug,Serialize,Deserialize,PartialEq,Eq)]
#[serde(rename_all="snake_case")]
pub enum AllowedDirection{Both,LongOnly,ShortOnly}
#[derive(Clone,Debug,Serialize,Deserialize,PartialEq,Eq)]
#[serde(rename_all="snake_case")]
pub enum FeeMode{Percentage,Fixed,Combined}
#[derive(Clone,Debug,Serialize,Deserialize,PartialEq,Eq)]
#[serde(rename_all="snake_case")]
pub enum TerminalPosition{MarkOnly,SettleAtLastClose}
#[derive(Clone,Debug,Serialize,Deserialize,PartialEq)]
#[serde(default,deny_unknown_fields)]
pub struct SimulationConfig{
    pub assumption_provenance:std::collections::BTreeMap<String,String>,
    pub start:Option<DateTime<Utc>>,pub end:Option<DateTime<Utc>>,pub parameters:Parameters,
    #[serde(with="decimal")] pub initial_capital:D,
    #[serde(with="decimal")] pub target_quantity:D,
    #[serde(with="decimal")] pub multiplier:D,
    pub allowed_direction:AllowedDirection,pub neutral_exit:bool,pub fee_mode:FeeMode,
    #[serde(with="decimal")] pub fee_rate:D,
    #[serde(with="decimal")] pub fixed_fee:D,
    pub slippage_ticks:u32,
    #[serde(with="decimal")] pub tick_size:D,
    pub settlement_precision:u32,
    #[serde(with="decimal")] pub maximum_exposure_ratio:D,
    pub terminal_position:TerminalPosition,
    #[serde(with="decimal")] pub risk_free_annual_rate:D,
}
impl Default for SimulationConfig{fn default()->Self{Self{assumption_provenance:["multiplier","tick_size","settlement_precision","target_quantity","fee_rate","fixed_fee"].into_iter().map(|key|(key.into(),"generic_simulation_default; contract specification unavailable".into())).collect(),start:None,end:None,parameters:Parameters::default(),initial_capital:d("10000"),target_quantity:D::one(),multiplier:D::one(),allowed_direction:AllowedDirection::Both,neutral_exit:true,fee_mode:FeeMode::Percentage,fee_rate:d("0.0002"),fixed_fee:D::zero(),slippage_ticks:0,tick_size:d("0.01"),settlement_precision:4,maximum_exposure_ratio:D::one(),terminal_position:TerminalPosition::MarkOnly,risk_free_annual_rate:D::zero()}}}
impl SimulationConfig{
    pub fn validate(&self)->Result<()>{ensure!(self.assumption_provenance.len()<=32&&self.assumption_provenance.iter().all(|(key,value)|key.len()<=80&&value.len()<=512&&!value.starts_with("verified_contract")),"configuration provenance is an assumption; verified contract metadata must come from authority input");self.parameters.validate()?;
        ensure!(self.initial_capital>D::zero()&&self.target_quantity>D::zero()&&self.multiplier>D::zero()&&self.tick_size>D::zero(),"capital, quantity, multiplier and tick must be positive");
        ensure!(self.fee_rate>=D::zero()&&self.fee_rate<=D::one()&&self.fixed_fee>=D::zero(),"invalid fee");
        ensure!(self.maximum_exposure_ratio>D::zero()&&self.maximum_exposure_ratio<=d("100"),"exposure ratio must be >0 and <=100");
        ensure!(self.settlement_precision<=28&&self.slippage_ticks<=100000,"invalid settlement precision or slippage count");
        ensure!(self.risk_free_annual_rate>=D::zero()&&self.risk_free_annual_rate<D::one(),"risk-free annual rate must be >=0 and <1");
        ensure!(self.start.zip(self.end).is_none_or(|(a,b)|a<b),"start must precede end");
        ensure!(self.fee_mode!=FeeMode::Percentage||self.fixed_fee.is_zero(),"percentage mode requires zero fixed fee");
        ensure!(self.fee_mode!=FeeMode::Fixed||self.fee_rate.is_zero(),"fixed mode requires zero percentage fee");Ok(())}
    pub fn hash(&self,input_hash:&str)->Result<String>{content_hash(&(EXECUTION_VERSION,CALCULATION_VERSION,input_hash,self))}
    fn money(&self,value:D)->D{round(&value,self.settlement_precision)}
    fn in_range(&self,time:DateTime<Utc>)->bool{self.start.is_none_or(|start|time>=start)&&self.end.is_none_or(|end|time<=end)}
}
#[derive(Clone,Debug,Serialize,Deserialize)]
pub struct Intent{pub decision_at:DateTime<Utc>,pub signal_as_of:DateTime<Utc>,pub direction:i8,pub input_version:String}
#[derive(Clone,Debug,Serialize,Deserialize)]
pub struct Fill{
    #[serde(with="super::quant::u64_text")]pub ordinal:u64,pub decision_at:DateTime<Utc>,pub signal_as_of:DateTime<Utc>,pub executed_at:DateTime<Utc>,pub kind:String,
    #[serde(with="decimal")] pub delta_quantity:D,#[serde(with="decimal")] pub base_price:D,
    #[serde(with="decimal")] pub fill_price:D,#[serde(with="decimal")] pub signed_notional:D,
    #[serde(with="decimal")] pub fee:D,#[serde(with="decimal")] pub slippage_cost:D,
    #[serde(with="decimal")] pub cash_after:D,#[serde(with="decimal")] pub quantity_after:D,
    pub input_version:String,pub terminal_valuation_assumption:bool,
}
#[derive(Clone,Debug,Serialize,Deserialize)]
pub struct ClosedTrade{
    pub opened_at:DateTime<Utc>,pub closed_at:DateTime<Utc>,pub direction:i8,
    #[serde(with="decimal")] pub quantity:D,#[serde(with="decimal")] pub entry_price:D,
    #[serde(with="decimal")] pub exit_price:D,#[serde(with="decimal")] pub net_profit:D,
    #[serde(with="decimal")] pub fees:D,#[serde(with="decimal")] pub slippage_cost:D,
    pub held_seconds:String,pub terminal_valuation_assumption:bool,
}
#[derive(Clone,Debug,Serialize,Deserialize)]
pub struct EquityPoint{pub at:DateTime<Utc>,#[serde(with="decimal")]pub mark:D,#[serde(with="decimal")]pub cash:D,#[serde(with="decimal")]pub quantity:D,#[serde(with="decimal")]pub equity:D,#[serde(with="decimal")]pub exposure:D,#[serde(with="decimal")]pub drawdown:D}
#[derive(Clone,Debug,Serialize,Deserialize)]
#[serde(tag="type",content="value",rename_all="snake_case")]
pub enum LedgerEvent{Fill(Fill),Trade(ClosedTrade),Equity(EquityPoint),Rejected{intent:Intent,reason:String},Unfilled{intent:Intent,reason:String},Decision(IndicatorPoint)}
#[derive(Clone,Debug)]struct Position{opened_at:DateTime<Utc>,price:D,quantity:D,notional:D,fee:D,slippage:D}
#[derive(Clone,Debug,Serialize,Deserialize)]
pub struct DefinedMetric{#[serde(with="optional_decimal")]pub value:Option<D>,pub unavailable_reason:Option<String>}
impl DefinedMetric{fn yes(v:D)->Self{Self{value:Some(v),unavailable_reason:None}}fn no(reason:&str)->Self{Self{value:None,unavailable_reason:Some(reason.into())}}}
#[derive(Clone,Debug,Serialize,Deserialize)]
pub struct RiskSampling{pub frequency:String,pub calendar:String,pub annual_factor:u32,pub samples:u64,pub elapsed_calendar_days:String,#[serde(with="decimal")]pub risk_free_annual_rate:D,pub sharpe:DefinedMetric,pub annualized_return:DefinedMetric}
#[derive(Clone,Debug,Serialize,Deserialize)]
pub struct SimulationSummary{
    pub execution_version:String,pub config:SimulationConfig,pub processed_bars:u64,pub fills:u64,pub closed_trades:u64,pub wins:u64,pub losses:u64,pub breakeven:u64,pub rejected_intents:u64,pub unfilled_intents:u64,
    #[serde(with="decimal")]pub cash:D,#[serde(with="decimal")]pub quantity:D,#[serde(with="decimal")]pub final_equity:D,
    #[serde(with="decimal")]pub realized_profit:D,#[serde(with="decimal")]pub unrealized_profit:D,#[serde(with="decimal")]pub return_percent:D,
    #[serde(with="decimal")]pub fees:D,#[serde(with="decimal")]pub slippage_cost:D,#[serde(with="decimal")]pub total_cost:D,
    #[serde(with="decimal")]pub maximum_drawdown:D,#[serde(with="decimal")]pub maximum_exposure:D,
    pub win_rate:DefinedMetric,pub average_win:DefinedMetric,pub average_loss:DefinedMetric,pub profit_factor:DefinedMetric,pub payoff_ratio:DefinedMetric,pub average_held_seconds:DefinedMetric,
    pub risk_sampling:RiskSampling,pub first_fill_at:Option<DateTime<Utc>>,pub last_mark_at:Option<DateTime<Utc>>,
    pub ledger_semantics:String,pub benchmark:Option<Value>,
}
// No floating division: quotient/remainder implement adverse tick rounding even at negative prices.
fn tick_price(value:&D,tick:&D,buy:bool)->D{
    let(a,ascl)=value.as_bigint_and_exponent();let(b,bscl)=tick.as_bigint_and_exponent();
    let exponent=bscl-ascl;let mut num=a;let mut den=b;
    let ten=|e:i64|num_bigint::BigInt::from(10_u8).pow(u32::try_from(e).unwrap());
    if exponent>=0{num*=ten(exponent);}else{den*=ten(-exponent);}
    let mut q=&num/&den;let r=&num%&den;
    if !r.is_zero(){if buy&&num.is_positive(){q+=1;}else if !buy&&num.is_negative(){q-=1;}}
    D::from(q)*tick
}
#[derive(Clone,Debug)]
pub struct Ledger{
    pub config:SimulationConfig,pub cash:D,pub quantity:D,pending:Option<Intent>,position:Option<Position>,events:Vec<LedgerEvent>,
    mark:D,equity:D,peak:D,max_drawdown:D,max_exposure:D,fees:D,slippage:D,realized:D,
    processed:u64,fills:u64,trades:u64,wins:u64,losses:u64,breakeven:u64,rejected:u64,unfilled:u64,gains:D,loss_amount:D,held_seconds:D,
    first_fill:Option<DateTime<Utc>>,last_mark:Option<DateTime<Utc>>,first_mark:Option<DateTime<Utc>>,
    daily_date:Option<NaiveDate>,daily_equity:Option<D>,previous_daily_equity:Option<D>,daily_count:u64,daily_sum:D,daily_squares:D,daily_gaps:bool,
}
impl Ledger{
    pub fn new(config:SimulationConfig)->Result<Self>{config.validate()?;let cash=config.initial_capital.clone();Ok(Self{config,cash:cash.clone(),quantity:D::zero(),pending:None,position:None,events:vec![],mark:D::zero(),equity:cash.clone(),peak:cash,max_drawdown:D::zero(),max_exposure:D::zero(),fees:D::zero(),slippage:D::zero(),realized:D::zero(),processed:0,fills:0,trades:0,wins:0,losses:0,breakeven:0,rejected:0,unfilled:0,gains:D::zero(),loss_amount:D::zero(),held_seconds:D::zero(),first_fill:None,last_mark:None,first_mark:None,daily_date:None,daily_equity:None,previous_daily_equity:None,daily_count:0,daily_sum:D::zero(),daily_squares:D::zero(),daily_gaps:false})}
    pub fn drain_events(&mut self)->Vec<LedgerEvent>{std::mem::take(&mut self.events)}
    pub fn intent(&mut self,intent:Intent){if !self.config.in_range(intent.decision_at){return;}let direction=match(self.config.allowed_direction.clone(),intent.direction){(AllowedDirection::LongOnly,-1)|(AllowedDirection::ShortOnly,1)=>0,(_,v)=>v};
        let direction=if direction==0&&!self.config.neutral_exit{if self.quantity>D::zero(){1}else if self.quantity<D::zero(){-1}else{0}}else{direction};
        if let Some(old)=self.pending.take(){if old.direction!=direction{self.unfilled+=1;self.events.push(LedgerEvent::Unfilled{intent:old,reason:"a later confirmed decision superseded this intention before a available open".into()});}}
        self.pending=Some(Intent{direction,..intent});}
    pub fn open(&mut self,at:DateTime<Utc>,base:&D)->Result<()>{if !self.config.in_range(at){return Ok(());}if self.pending.as_ref().is_none_or(|p|p.decision_at>at){return Ok(());}let intent=self.pending.take().unwrap();let desired=&self.config.target_quantity*D::from(intent.direction);
        if desired==self.quantity{return Ok(());}if !self.quantity.is_zero(){self.fill(&intent,at,base,-self.quantity.clone(),"close",false)?;}
        if !desired.is_zero(){let price=self.price(base,desired>D::zero());let exposure=(&desired*&price*&self.config.multiplier).abs();let equity=&self.cash+&self.quantity*base*&self.config.multiplier;
            // Short sale proceeds and a negative-price buy cannot inflate the configured opening capacity.
            let limit=self.config.initial_capital.clone().min(equity.max(D::zero()))*&self.config.maximum_exposure_ratio;
            let fee=self.fee(&(&desired*&price*&self.config.multiplier));
            if exposure+&fee>limit{self.rejected+=1;self.events.push(LedgerEvent::Rejected{intent,reason:"absolute opening notional plus fee exceeds configured equity/initial-capital exposure limit".into()});}
            else{self.fill(&intent,at,base,desired,"open",false)?;}}
        Ok(())}
    fn price(&self,base:&D,buy:bool)->D{let slip=&self.config.tick_size*D::from(self.config.slippage_ticks);tick_price(&(if buy{base+slip}else{base-slip}),&self.config.tick_size,buy)}
    fn fee(&self,notional:&D)->D{let percent=if self.config.fee_mode==FeeMode::Fixed{D::zero()}else{notional.abs()*&self.config.fee_rate};let fixed=if self.config.fee_mode==FeeMode::Percentage{D::zero()}else{self.config.fixed_fee.clone()};self.config.money(percent+fixed)}
    fn fill(&mut self,intent:&Intent,at:DateTime<Utc>,base:&D,delta:D,kind:&str,terminal:bool)->Result<()>{let price=self.price(base,delta>D::zero());let notional=self.config.money(&delta*&price*&self.config.multiplier);let fee=self.fee(&notional);let slip=self.config.money((&price-base)*&delta*&self.config.multiplier);ensure!(slip>=D::zero(),"adverse slippage must be nonnegative");self.cash-=&notional+&fee;self.quantity+=&delta;self.fees+=&fee;self.slippage+=&slip;self.fills+=1;self.first_fill.get_or_insert(at);
        if kind=="open"{self.position=Some(Position{opened_at:at,price:price.clone(),quantity:delta.clone(),notional:notional.clone(),fee:fee.clone(),slippage:slip.clone()});}
        else{let position=self.position.take().expect("close has an open position");let profit=-(&position.notional+&notional+&position.fee+&fee);self.realized+=&profit;self.trades+=1;if profit>D::zero(){self.wins+=1;self.gains+=&profit;}else if profit<D::zero(){self.losses+=1;self.loss_amount-= &profit;}else{self.breakeven+=1;}let held=at.signed_duration_since(position.opened_at).num_seconds();self.held_seconds+=D::from(held);self.events.push(LedgerEvent::Trade(ClosedTrade{opened_at:position.opened_at,closed_at:at,direction:if position.quantity>D::zero(){1}else{-1},quantity:position.quantity.abs(),entry_price:position.price,exit_price:price.clone(),net_profit:profit,fees:&position.fee+&fee,slippage_cost:position.slippage+&slip,held_seconds:held.to_string(),terminal_valuation_assumption:terminal}));}
        self.events.push(LedgerEvent::Fill(Fill{ordinal:self.fills,decision_at:intent.decision_at,signal_as_of:intent.signal_as_of,executed_at:at,kind:kind.into(),delta_quantity:delta,base_price:base.clone(),fill_price:price,signed_notional:notional,fee,slippage_cost:slip,cash_after:self.cash.clone(),quantity_after:self.quantity.clone(),input_version:intent.input_version.clone(),terminal_valuation_assumption:terminal}));Ok(())}
    fn finalize_day(&mut self){if let Some(current)=self.daily_equity.take(){if let Some(previous)=self.previous_daily_equity.replace(current.clone()){if previous>D::zero(){let r=div(&current,&previous)-D::one();self.daily_sum+=&r;self.daily_squares+=&r*&r;self.daily_count+=1;}else{self.daily_gaps=true;}}}}
    pub fn mark(&mut self,at:DateTime<Utc>,price:&D){if !self.config.in_range(at){return;}self.processed+=1;self.mark=price.clone();self.equity=&self.cash+&self.quantity*price*&self.config.multiplier;self.peak=self.peak.clone().max(self.equity.clone());let drawdown=if self.peak>D::zero(){div(&(&self.peak-&self.equity),&self.peak)}else{D::zero()};self.max_drawdown=self.max_drawdown.clone().max(drawdown.clone());let exposure=(&self.quantity*price*&self.config.multiplier).abs();self.max_exposure=self.max_exposure.clone().max(exposure.clone());self.first_mark.get_or_insert(at);self.last_mark=Some(at);
        let date=at.date_naive();if let Some(old)=self.daily_date{if date!=old{if date.signed_duration_since(old).num_days()!=1{self.daily_gaps=true;}self.finalize_day();}}
        self.daily_date=Some(date);self.daily_equity=Some(self.equity.clone());self.events.push(LedgerEvent::Equity(EquityPoint{at,mark:price.clone(),cash:self.cash.clone(),quantity:self.quantity.clone(),equity:self.equity.clone(),exposure,drawdown}));}
    pub fn finish(mut self)->Result<(SimulationSummary,Vec<LedgerEvent>)>{if let Some(intent)=self.pending.take(){if intent.direction!=if self.quantity>D::zero(){1}else if self.quantity<D::zero(){-1}else{0}{self.unfilled+=1;self.events.push(LedgerEvent::Unfilled{intent,reason:"no subsequent available bar open in the selected range".into()});}}
        if self.config.terminal_position==TerminalPosition::SettleAtLastClose&&!self.quantity.is_zero(){let at=self.last_mark.unwrap();let intent=Intent{decision_at:at,signal_as_of:at,direction:0,input_version:"terminal-valuation-assumption".into()};let mark=self.mark.clone();self.fill(&intent,at,&mark,-self.quantity.clone(),"terminal_settlement",true)?;self.equity=self.cash.clone();self.peak=self.peak.clone().max(self.equity.clone());if self.peak>D::zero(){self.max_drawdown=self.max_drawdown.clone().max(div(&(&self.peak-&self.equity),&self.peak));}self.daily_equity=Some(self.equity.clone());self.events.push(LedgerEvent::Equity(EquityPoint{at,mark,cash:self.cash.clone(),quantity:self.quantity.clone(),equity:self.equity.clone(),exposure:D::zero(),drawdown:if self.peak>D::zero(){div(&(&self.peak-&self.equity),&self.peak)}else{D::zero()}}));}
        self.finalize_day();let elapsed=self.first_mark.zip(self.last_mark).map_or(0,|(a,b)|b.signed_duration_since(a).num_days());let mean=if self.daily_count>0{div(&self.daily_sum,&D::from(self.daily_count))}else{D::zero()};let std=super::exact::population_deviation(&self.daily_sum,&self.daily_squares,self.daily_count as usize).unwrap_or_default();
        let sharpe=if self.daily_count<30{DefinedMetric::no("requires at least 30 daily close returns")}else if self.daily_gaps{DefinedMetric::no("UTC calendar daily samples have gaps or nonpositive prior equity; no trading-calendar annual factor is inferred")}else if std.is_zero(){DefinedMetric::no("daily return variance is zero")}else{DefinedMetric::yes(div(&(&mean-div(&self.config.risk_free_annual_rate,&d("365"))),&std)*sqrt(&d("365")).unwrap())};
        // Annual return is deliberately undefined until a full calendar year; never annualize minute bar counts.
        let annualized=if elapsed<365{DefinedMetric::no("requires a full 365 elapsed calendar days")}else if self.equity<=D::zero(){DefinedMetric::no("nonpositive final equity cannot be compounded")}else{let log=super::exact::ln_ratio(&self.equity,&self.config.initial_capital).unwrap();let exponent=div(&(log*365_i64),&D::from(elapsed));match super::exact::exp(&exponent){Some(value)=>DefinedMetric::yes(value-D::one()),None=>DefinedMetric::no("compounding exponent exceeds the supported statistics domain") }};
        let ratio=|a:&D,b:&D,reason:&str|if b.is_zero(){DefinedMetric::no(reason)}else{DefinedMetric::yes(div(a,b))};let average_win=ratio(&self.gains,&D::from(self.wins),"no profitable closed trades");let average_loss=ratio(&self.loss_amount,&D::from(self.losses),"no losing closed trades");let payoff=match(&average_win.value,&average_loss.value){(Some(a),Some(b))=>ratio(a,b,"average loss is zero"),_=>DefinedMetric::no("requires both profitable and losing closed trades")};
        let unrealized=&self.equity-&self.config.initial_capital-&self.realized;let summary=SimulationSummary{execution_version:EXECUTION_VERSION.into(),config:self.config.clone(),processed_bars:self.processed,fills:self.fills,closed_trades:self.trades,wins:self.wins,losses:self.losses,breakeven:self.breakeven,rejected_intents:self.rejected,unfilled_intents:self.unfilled,cash:self.cash,quantity:self.quantity,final_equity:self.equity.clone(),realized_profit:self.realized,unrealized_profit:unrealized,return_percent:(div(&self.equity,&self.config.initial_capital)-D::one())*100_i64,fees:self.fees.clone(),slippage_cost:self.slippage.clone(),total_cost:self.fees+self.slippage,maximum_drawdown:self.max_drawdown,maximum_exposure:self.max_exposure,win_rate:ratio(&D::from(self.wins),&D::from(self.trades),"no closed trades"),average_win,average_loss,profit_factor:ratio(&self.gains,&self.loss_amount,"no losing trades; profit factor has no finite denominator"),payoff_ratio:payoff,average_held_seconds:ratio(&self.held_seconds,&D::from(self.trades),"no closed trades"),risk_sampling:RiskSampling{frequency:"UTC calendar daily final marked equity".into(),calendar:"UTC calendar; gaps disable Sharpe rather than guessing exchange days".into(),annual_factor:365,samples:self.daily_count,elapsed_calendar_days:elapsed.to_string(),risk_free_annual_rate:self.config.risk_free_annual_rate,sharpe,annualized_return:annualized},first_fill_at:self.first_fill,last_mark_at:self.last_mark,ledger_semantics:"signed price-series cash ledger; no exchange margin/roll/funding model; final revision history is not historical observability".into(),benchmark:None};Ok((summary,self.events))}
}
/// Stream one immutable final bar at a time; warmup is outside the execution range.
pub struct Simulator{pub evaluator:super::evaluator::Evaluator,pub ledger:Ledger,pub benchmark:Ledger,benchmark_started:bool,semantics:HistorySemantics}
impl Simulator{
    pub fn new(config:SimulationConfig,semantics:HistorySemantics)->Result<Self>{let evaluator=super::evaluator::Evaluator::new(config.parameters.clone())?;let mut baseline=config.clone();baseline.allowed_direction=AllowedDirection::LongOnly;baseline.neutral_exit=false;Ok(Self{evaluator,ledger:Ledger::new(config)?,benchmark:Ledger::new(baseline)?,benchmark_started:false,semantics})}
    pub fn push(&mut self,bar:QuantBar,input_version:String)->Result<Vec<LedgerEvent>>{self.ledger.open(bar.open_time,&bar.open)?;if !self.benchmark_started&&self.ledger.first_fill.is_some(){self.benchmark.intent(Intent{decision_at:bar.open_time,signal_as_of:bar.open_time,direction:1,input_version:input_version.clone()});self.benchmark.open(bar.open_time,&bar.open)?;self.benchmark_started=true;}
        self.benchmark.drain_events();self.ledger.mark(bar.bucket_end,&bar.close);self.benchmark.mark(bar.bucket_end,&bar.close);let point=self.evaluator.push_execution(bar,&self.semantics)?;let intent=Intent{decision_at:point.decision_at,signal_as_of:point.as_of,direction:point.direction,input_version};self.ledger.intent(intent);let mut events=self.ledger.drain_events();events.push(LedgerEvent::Decision(point));self.benchmark.drain_events();Ok(events)}
    pub fn finish(self)->Result<(SimulationSummary,Vec<LedgerEvent>)>{let (mut summary,events)=self.ledger.finish()?;let (benchmark,_)=self.benchmark.finish()?;summary.benchmark=Some(serde_json::json!({"first_fill_at":benchmark.first_fill_at,"final_equity":text(&benchmark.final_equity),"return_percent":text(&benchmark.return_percent),"fees":text(&benchmark.fees),"slippage_cost":text(&benchmark.slippage_cost),"unavailable_reason":if self.benchmark_started{None}else{Some("strategy had no feasible fill; no comparable entry point")},"policy":"same first strategy fill open, quantity, capital, multiplier, costs and terminal policy; long buy-and-hold"}));Ok((summary,events))}
}

#[cfg(test)]mod tests{
use super::*;
fn time(index:i64)->DateTime<Utc>{DateTime::from_timestamp(1_700_000_000+index*60,0).unwrap()}
fn intent(index:i64,direction:i8)->Intent{Intent{decision_at:time(index),signal_as_of:time(index-1),direction,input_version:format!("prefix-{index}")}}
#[test]fn independent_four_fill_oracle(){let mut config=SimulationConfig::default();config.target_quantity=d("2");config.multiplier=d("10");config.tick_size=d("0.5");config.slippage_ticks=2;config.fixed_fee=d("1");config.fee_mode=FeeMode::Combined;let mut ledger=Ledger::new(config).unwrap();ledger.mark(time(1),&d("100"));ledger.intent(intent(1,1));ledger.open(time(1),&d("103")).unwrap();ledger.mark(time(2),&d("106"));ledger.intent(intent(2,-1));ledger.open(time(2),&d("99")).unwrap();ledger.mark(time(3),&d("96"));ledger.intent(intent(3,0));ledger.open(time(3),&d("97")).unwrap();ledger.mark(time(4),&d("101"));ledger.intent(intent(4,-1));let(s,events)=ledger.finish().unwrap();let fills:Vec<_>=events.iter().filter_map(|e|if let LedgerEvent::Fill(f)=e{Some(f)}else{None}).collect();assert_eq!(fills.len(),4);assert_eq!(fills.iter().map(|f|text(&f.cash_after)).collect::<Vec<_>>(),["7918.584","9877.192","11835.8","9874.408"]);assert_eq!(s.realized_profit,d("-125.592"));assert_eq!(s.final_equity,d("9874.408"));assert_eq!(s.fees,d("5.592"));assert_eq!(s.slippage_cost,d("80"));assert_eq!(s.return_percent,d("-1.25592"));assert_eq!(s.unfilled_intents,1);assert_eq!(s.profit_factor.value,Some(D::zero()));assert_eq!(s.maximum_drawdown,div(&d("164.176"),&d("10038.584")));assert_eq!(s.unrealized_profit,D::zero());}
#[test]fn signed_negative_price_oracle(){let mut c=SimulationConfig::default();c.initial_capital=d("1000");c.target_quantity=d("2");c.tick_size=d("0.5");c.slippage_ticks=1;c.fee_mode=FeeMode::Fixed;c.fee_rate=D::zero();c.fixed_fee=d("0.1");let mut l=Ledger::new(c).unwrap();l.intent(intent(1,1));l.open(time(1),&d("-2")).unwrap();assert_eq!(l.cash,d("1002.9"));l.mark(time(2),&d("-1"));assert_eq!(l.equity,d("1000.9"));l.intent(intent(2,0));l.open(time(2),&d("1.5")).unwrap();l.mark(time(3),&d("1.5"));let(s,_)=l.finish().unwrap();assert_eq!(s.cash,d("1004.8"));assert_eq!(s.realized_profit,d("4.8"));assert!(s.fees>D::zero());}
#[test]fn zero_trades_and_wide_cash_are_defined(){let mut c=SimulationConfig::default();c.initial_capital=d("79228162514264337593543950335.0000000000000000000000000001");let l=Ledger::new(c.clone()).unwrap();let(s,_)=l.finish().unwrap();assert_eq!(s.cash,c.initial_capital);assert!(s.win_rate.value.is_none());assert!(s.profit_factor.value.is_none());assert_eq!(s.return_percent,D::zero());}
#[test]fn adverse_tick_rounding_includes_negative_values(){assert_eq!(tick_price(&d("-1.01"),&d(".5"),true),d("-1"));assert_eq!(tick_price(&d("-1.01"),&d(".5"),false),d("-1.5"));assert_eq!(tick_price(&d("1.01"),&d(".5"),true),d("1.5"));assert_eq!(tick_price(&d("1.01"),&d(".5"),false),d("1"));}
#[test]fn terminal_settlement_is_explicit_and_costed(){let mut c=SimulationConfig::default();c.fee_mode=FeeMode::Fixed;c.fee_rate=D::zero();c.fixed_fee=d("1");c.terminal_position=TerminalPosition::SettleAtLastClose;let mut l=Ledger::new(c).unwrap();l.intent(intent(1,1));l.open(time(1),&d("100")).unwrap();l.mark(time(2),&d("110"));let(s,events)=l.finish().unwrap();assert_eq!(s.realized_profit,d("8"));assert_eq!(s.quantity,D::zero());assert_eq!(s.fees,d("2"));assert!(events.iter().any(|e|matches!(e,LedgerEvent::Fill(f)if f.terminal_valuation_assumption)));}
#[test]fn fee_risk_rejection_and_late_decision(){let mut c=SimulationConfig::default();c.initial_capital=d("100");c.target_quantity=d("2");let mut l=Ledger::new(c).unwrap();l.intent(intent(2,1));l.open(time(1),&d("100")).unwrap();assert_eq!(l.fills,0);l.open(time(2),&d("100")).unwrap();assert_eq!(l.rejected,1);assert_eq!(l.quantity,D::zero());}
}
#[cfg(test)]mod terminal_drawdown_tests{use super::*;#[test]fn recovered_terminal_point_reports_current_not_historical_maximum(){let mut c=SimulationConfig::default();c.fee_rate=D::zero();c.terminal_position=TerminalPosition::SettleAtLastClose;let t=DateTime::from_timestamp(1_700_000_000,0).unwrap();let mut l=Ledger::new(c).unwrap();l.intent(Intent{decision_at:t,signal_as_of:t,direction:1,input_version:"fixture".into()});l.open(t,&d("100")).unwrap();l.mark(t+chrono::Duration::minutes(1),&d("50"));l.mark(t+chrono::Duration::minutes(2),&d("120"));let(summary,events)=l.finish().unwrap();assert!(summary.maximum_drawdown>D::zero());let final_point=events.iter().rev().find_map(|event|if let LedgerEvent::Equity(point)=event{Some(point)}else{None}).unwrap();assert_eq!(final_point.drawdown,D::zero());}}
