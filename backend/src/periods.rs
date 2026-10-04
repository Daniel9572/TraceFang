//! Exchange-session-aware period boundaries and complete replacement projections.
use crate::domain::{
    CoreError, CoreResult, Decimal, Instrument, SourceMetadata, Timestamp, isoformat, require,
};
use crate::events::{BarState, RealtimeBar};
use chrono::{
    Datelike, Duration, LocalResult, NaiveDate, NaiveDateTime, NaiveTime, TimeZone, Timelike, Utc,
};
use chrono_tz::Tz;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{collections::BTreeMap, str::FromStr};

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub enum Period {
    #[serde(rename = "timeline")]
    Timeline,
    #[serde(rename = "1s")]
    S1,
    #[serde(rename = "1m")]
    M1,
    #[serde(rename = "3m")]
    M3,
    #[serde(rename = "5m")]
    M5,
    #[serde(rename = "10m")]
    M10,
    #[serde(rename = "15m")]
    M15,
    #[serde(rename = "30m")]
    M30,
    #[serde(rename = "1h")]
    H1,
    #[serde(rename = "2h")]
    H2,
    #[serde(rename = "4h")]
    H4,
    #[serde(rename = "6h")]
    H6,
    #[serde(rename = "8h")]
    H8,
    #[serde(rename = "12h")]
    H12,
    #[serde(rename = "1d")]
    D1,
    #[serde(rename = "1w")]
    W1,
    #[serde(rename = "1mo")]
    Mo1,
    #[serde(rename = "1q")]
    Q1,
    #[serde(rename = "1y")]
    Y1,
}
impl Period {
    pub const ALL: [Self; 19] = [
        Self::Timeline,
        Self::S1,
        Self::M1,
        Self::M3,
        Self::M5,
        Self::M10,
        Self::M15,
        Self::M30,
        Self::H1,
        Self::H2,
        Self::H4,
        Self::H6,
        Self::H8,
        Self::H12,
        Self::D1,
        Self::W1,
        Self::Mo1,
        Self::Q1,
        Self::Y1,
    ];
    pub fn parse(value: &str) -> CoreResult<Self> {
        value.parse()
    }
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Timeline => "timeline",
            Self::S1 => "1s",
            Self::M1 => "1m",
            Self::M3 => "3m",
            Self::M5 => "5m",
            Self::M10 => "10m",
            Self::M15 => "15m",
            Self::M30 => "30m",
            Self::H1 => "1h",
            Self::H2 => "2h",
            Self::H4 => "4h",
            Self::H6 => "6h",
            Self::H8 => "8h",
            Self::H12 => "12h",
            Self::D1 => "1d",
            Self::W1 => "1w",
            Self::Mo1 => "1mo",
            Self::Q1 => "1q",
            Self::Y1 => "1y",
        }
    }
    pub fn seconds(self) -> Option<i64> {
        match self {
            Self::Timeline | Self::S1 => Some(1),
            Self::M1 => Some(60),
            Self::M3 => Some(180),
            Self::M5 => Some(300),
            Self::M10 => Some(600),
            Self::M15 => Some(900),
            Self::M30 => Some(1800),
            Self::H1 => Some(3600),
            Self::H2 => Some(7200),
            Self::H4 => Some(14400),
            Self::H6 => Some(21600),
            Self::H8 => Some(28800),
            Self::H12 => Some(43200),
            _ => None,
        }
    }
    pub fn is_base(self) -> bool {
        matches!(self, Self::Timeline | Self::S1 | Self::M1)
    }
}
impl FromStr for Period {
    type Err = CoreError;
    fn from_str(value: &str) -> CoreResult<Self> {
        Self::ALL
            .into_iter()
            .find(|p| p.as_str() == value)
            .ok_or_else(|| CoreError(format!("unsupported chart period {value:?}")))
    }
}
impl std::fmt::Display for Period {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TradingDayRule {
    SessionStart,
    SessionEnd,
    Shfe,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TradingSession {
    pub weekday: u32,
    pub open: String,
    pub close: String,
    pub close_day_offset: i64,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct MarketSchedule {
    pub time_zone: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub trading_day_rule: Option<TradingDayRule>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reference: Option<String>,
    pub sessions: Vec<TradingSession>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub authority: Option<CalendarAuthority>,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
pub struct CalendarAuthority {
    #[serde(default,skip_serializing_if="Option::is_none")]
    pub date_exceptions:Option<DateExceptions>,
    #[serde(default,skip_serializing_if="Vec::is_empty")]
    pub absolute_days:Vec<AbsoluteTradingDay>,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DateExceptions {
    pub verified_years:Vec<i32>,
    pub closed_date_ranges:Vec<[NaiveDate;2]>,
    pub no_evening_start_dates:Vec<NaiveDate>,
    pub provenance:Value,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AbsoluteTradingDay {
    pub market:String,pub code:String,pub trade_date:NaiveDate,
    pub continuous_sessions:Vec<AbsoluteSession>,
    pub raw_body_sha256:String,
    #[serde(with="crate::persistence_contract::i64_string")]
    pub received_at_ns:i64,
    #[serde(default,with="crate::persistence_contract::optional_i64_string")]
    pub accepted_at_ns:Option<i64>,
    #[serde(default,skip_serializing_if="Option::is_none")]
    pub capture_position:Option<crate::persistence_contract::CapturePosition>,
    pub provenance:Value,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AbsoluteSession {pub start:Timestamp,pub end:Timestamp}
#[derive(Debug,Clone,PartialEq,Eq,Serialize,Deserialize)]
pub struct CapturedSourceCalendar {pub source_id:String,pub symbol:String,pub day:AbsoluteTradingDay}
impl MarketSchedule {
    pub fn validate(&self) -> CoreResult<()> {
        self.zone()?;
        if let Some(authority)=&self.authority {
            require(authority.absolute_days.len()<=4096,"absolute calendar exceeds bounded dates")?;
            for day in &authority.absolute_days {
                require(!day.market.is_empty() && !day.code.is_empty() && day.raw_body_sha256.len()==64 && day.raw_body_sha256.bytes().all(|b|b.is_ascii_hexdigit()),"invalid exact source calendar identity")?;
                require(!day.continuous_sessions.is_empty() && day.continuous_sessions.len()<=32,"absolute calendar session count invalid")?;
                let mut previous=None;
                for session in &day.continuous_sessions {require(session.start<session.end && previous.is_none_or(|end|end<=session.start),"absolute sessions overlap or are unordered")?;previous=Some(session.end);}
            }
            if let Some(exceptions)=&authority.date_exceptions {
                require(!exceptions.verified_years.is_empty() && exceptions.verified_years.len()<=32,"holiday year coverage invalid")?;
                for range in &exceptions.closed_date_ranges {require(range[0]<=range[1],"holiday range inverted")?;}
            }
        }
        for session in &self.sessions {
            require(
                session.weekday <= 6,
                "session weekday must be between zero and six",
            )?;
            clock(&session.open)?;
            clock(&session.close)?;
            require(
                (0..=2).contains(&session.close_day_offset),
                "session close day offset must be between zero and two",
            )?;
        }
        Ok(())
    }
    fn zone(&self) -> CoreResult<Tz> {
        self.time_zone
            .parse()
            .map_err(|_| CoreError(format!("invalid market time zone {:?}", self.time_zone)))
    }
    fn rule(&self) -> TradingDayRule {
        self.trading_day_rule.unwrap_or(TradingDayRule::SessionEnd)
    }
}
fn has_schedule(schedule:&MarketSchedule)->bool{!schedule.sessions.is_empty() || schedule.authority.is_some()}
fn closed_date(date:NaiveDate,exceptions:&DateExceptions)->bool {date.weekday().number_from_monday()>=6 || exceptions.closed_date_ranges.iter().any(|r|r[0]<=date && date<=r[1])}
pub fn calendar_date_verified(date:NaiveDate,schedule:&MarketSchedule)->bool {
    schedule.authority.as_ref().is_none_or(|a|a.absolute_days.iter().any(|d|d.trade_date==date) || a.date_exceptions.as_ref().is_some_and(|e|e.verified_years.contains(&date.year())))
}
/// Calendar authority covers the whole display period, not just the first
/// surviving minute. A one-date vendor calendar cannot confirm a month/week.
pub fn calendar_bucket_verified(at:Timestamp,period:Period,schedule:Option<&MarketSchedule>)->CoreResult<bool>{
    // Base facts keep their original finality even outside calendar coverage.
    if period.is_base(){return Ok(true);}
    let Some(schedule)=schedule.filter(|s|has_schedule(s)) else{return Ok(true);};
    let Some(occurrence)=session_occurrence(at,Some(schedule))? else{return Ok(false);};
    if period.seconds().is_some(){return Ok(calendar_date_verified(occurrence.trading_date,schedule));}
    let (mut day,end)=calendar_bounds(occurrence.trading_date,period)?;
    while day<end {if !calendar_date_verified(day,schedule){return Ok(false);}day=date_add(day,1)?;}
    Ok(true)
}
/// Actual clock of the captured date/session evidence needed by this bucket.
/// Static annual policies have no historical knowledge clock to invent.
pub fn calendar_evidence_known_at(at:Timestamp,period:Period,schedule:Option<&MarketSchedule>)->CoreResult<Option<Timestamp>> {
    if period.is_base(){return Ok(None);}
    let Some(schedule)=schedule else{return Ok(None);};let Some(authority)=&schedule.authority else{return Ok(None);};
    let Some(occurrence)=session_occurrence(at,Some(schedule))? else{return Ok(None);};
    let (start,end)=if period.seconds().is_some(){(occurrence.trading_date,date_add(occurrence.trading_date,1)?)}else{calendar_bounds(occurrence.trading_date,period)?};
    Ok(authority.absolute_days.iter().filter(|day|day.trade_date>=start && day.trade_date<end).map(|day|chrono::DateTime::from_timestamp_nanos(day.received_at_ns.max(day.accepted_at_ns.unwrap_or(i64::MIN)))).max())
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Bucket {
    pub key: String,
    pub start: Timestamp,
    pub end: Timestamp,
    pub evidence_start: Option<Timestamp>,
    pub evidence_end: Option<Timestamp>,
}
impl Bucket {
    pub fn input_start(&self) -> Timestamp {
        self.evidence_start.unwrap_or(self.start)
    }
    pub fn input_end(&self) -> Timestamp {
        self.evidence_end.unwrap_or(self.end)
    }
}
#[derive(Debug, Clone)]
struct SessionOccurrence {
    start: Timestamp,
    end: Timestamp,
    trading_date: NaiveDate,
    local_start: NaiveDateTime,
    local_end: NaiveDateTime,
}
fn clock(value: &str) -> CoreResult<NaiveTime> {
    NaiveTime::parse_from_str(value, "%H:%M")
        .map_err(|_| CoreError(format!("invalid session clock {value:?}")))
}
fn date_at(year: i32, month: u32, day: u32) -> CoreResult<NaiveDate> {
    NaiveDate::from_ymd_opt(year, month, day)
        .ok_or_else(|| CoreError("calendar date is out of range".into()))
}
fn date_add(date: NaiveDate, days: i64) -> CoreResult<NaiveDate> {
    date.checked_add_signed(Duration::days(days))
        .ok_or_else(|| CoreError("calendar date is out of range".into()))
}
fn midnight(date: NaiveDate) -> NaiveDateTime {
    date.and_time(NaiveTime::MIN)
}
/// Match Python zoneinfo fold=0. For a skipped wall time Python uses its pre-transition offset.
fn localize(zone: Tz, naive: NaiveDateTime) -> CoreResult<Timestamp> {
    match zone.from_local_datetime(&naive) {
        LocalResult::Single(value) => Ok(value.with_timezone(&Utc)),
        LocalResult::Ambiguous(first, second) => Ok(first.min(second).with_timezone(&Utc)),
        LocalResult::None => {
            for minutes in 1..=180 {
                if let Some(previous) = zone
                    .from_local_datetime(&(naive - Duration::minutes(minutes)))
                    .earliest()
                {
                    return previous
                        .offset()
                        .fix()
                        .from_local_datetime(&naive)
                        .single()
                        .map(|v| v.with_timezone(&Utc))
                        .ok_or_else(|| CoreError("cannot resolve market wall time".into()));
                }
            }
            Err(CoreError("cannot resolve market wall time".into()))
        }
    }
}
use chrono::Offset;
fn trading_date(
    start: NaiveDateTime,
    end: NaiveDateTime,
    rule: TradingDayRule,
) -> CoreResult<NaiveDate> {
    match rule {
        TradingDayRule::Shfe if start.hour() >= 18 => {
            let mut date = date_add(start.date(), 1)?;
            while date.weekday().number_from_monday() >= 6 {
                date = date_add(date, 1)?;
            }
            Ok(date)
        }
        TradingDayRule::Shfe | TradingDayRule::SessionStart => Ok(start.date()),
        TradingDayRule::SessionEnd => Ok((end - Duration::microseconds(1)).date()),
    }
}
fn session_times(
    date: NaiveDate,
    session: &TradingSession,
) -> CoreResult<(NaiveDateTime, NaiveDateTime)> {
    Ok((
        date.and_time(clock(&session.open)?),
        date_add(date, session.close_day_offset)?.and_time(clock(&session.close)?),
    ))
}
fn occurrences_on(date:NaiveDate,schedule:&MarketSchedule)->CoreResult<Vec<SessionOccurrence>> {
    let zone=schedule.zone()?;let mut result=Vec::new();
    if let Some(authority)=&schedule.authority {
        for day in &authority.absolute_days {
            for session in &day.continuous_sessions {
                let local_start=session.start.with_timezone(&zone).naive_local();
                if local_start.date()==date {result.push(SessionOccurrence{start:session.start,end:session.end,trading_date:day.trade_date,local_start,local_end:session.end.with_timezone(&zone).naive_local()});}
            }
        }
        if authority.date_exceptions.is_none(){return Ok(result);}
    }
    let exceptions=schedule.authority.as_ref().and_then(|a|a.date_exceptions.as_ref());
    if exceptions.is_some_and(|e|!e.verified_years.contains(&date.year()) || closed_date(date,e)){return Ok(result);}
    for session in &schedule.sessions {
        if date.weekday().num_days_from_sunday()!=session.weekday{continue;}
        let (start,end)=session_times(date,session)?;
        if exceptions.is_some_and(|e|start.hour()>=18 && e.no_evening_start_dates.contains(&date)){continue;}
        let trading_date=if schedule.rule()==TradingDayRule::Shfe && start.hour()>=18 {
            let mut target=date_add(date,1)?;let mut found=false;
            for _ in 0..32 {
                if exceptions.is_some_and(|e|!e.verified_years.contains(&target.year())){break;}
                if target.weekday().number_from_monday()<6 && exceptions.is_none_or(|e|!closed_date(target,e)){found=true;break;}
                target=date_add(target,1)?;
            }if !found{continue;}target
        }else{trading_date(start,end,schedule.rule())?};
        if schedule.authority.as_ref().is_some_and(|a|a.absolute_days.iter().any(|d|d.trade_date==trading_date)){continue;}
        result.push(SessionOccurrence {start:localize(zone,start)?,end:localize(zone,end)?,trading_date,local_start:start,local_end:end});
    }result.sort_by_key(|v|v.start);Ok(result)
}
fn session_occurrence(at:Timestamp,schedule:Option<&MarketSchedule>)->CoreResult<Option<SessionOccurrence>> {
    let Some(schedule)=schedule.filter(|s|has_schedule(s)) else{return Ok(None);};
    let date=at.with_timezone(&schedule.zone()?).date_naive();
    for offset in 0..4 {for occurrence in occurrences_on(date_add(date,-offset)?,schedule)? {if occurrence.start<=at && at<occurrence.end{return Ok(Some(occurrence));}}}
    Ok(None)
}
pub fn calendar_instant_verified(at:Timestamp,schedule:Option<&MarketSchedule>)->CoreResult<bool> {
    let Some(schedule)=schedule else{return Ok(true);};
    if let Some(occurrence)=session_occurrence(at,Some(schedule))?{return Ok(calendar_date_verified(occurrence.trading_date,schedule));}
    let date=at.with_timezone(&schedule.zone()?).date_naive();
    if calendar_date_verified(date,schedule){return Ok(true);}
    Ok(schedule.authority.as_ref().is_some_and(|a|a.absolute_days.iter().any(|d|d.continuous_sessions.first().zip(d.continuous_sessions.last()).is_some_and(|(first,last)|first.start<=at && at<last.end))))
}
/// Intervals where an absence of a declared session is actually known. An
/// unverified year/date is distinct from a known closed gap.
pub fn calendar_known_ranges(start:Timestamp,end:Timestamp,schedule:Option<&MarketSchedule>)->CoreResult<Vec<(Timestamp,Timestamp)>> {
    require(start<=end,"inverted calendar coverage range")?;
    let Some(schedule)=schedule else{return Ok(vec![(start,end)]);};
    let Some(authority)=&schedule.authority else{return Ok(vec![(start,end)]);};
    let zone=schedule.zone()?;let mut ranges=Vec::new();
    if let Some(exceptions)=&authority.date_exceptions {for year in &exceptions.verified_years {
        ranges.push((localize(zone,midnight(date_at(*year,1,1)?))?,localize(zone,midnight(date_at(year+1,1,1)?))?));
    }}
    for day in &authority.absolute_days {
        ranges.push((localize(zone,midnight(day.trade_date))?,localize(zone,midnight(date_add(day.trade_date,1)?))?));
        if let Some((first,last))=day.continuous_sessions.first().zip(day.continuous_sessions.last()){ranges.push((first.start,last.end));}
    }
    ranges.sort_unstable();let mut merged:Vec<(Timestamp,Timestamp)>=vec![];
    for (lo,hi) in ranges {let lo=lo.max(start);let hi=hi.min(end);if lo>=hi{continue;}
        if let Some(last)=merged.last_mut(){if lo<=last.1{last.1=last.1.max(hi);continue;}}merged.push((lo,hi));
    }Ok(merged)
}
/// Independent consumers can inspect the declared sessions for a trading date.
pub fn trading_day_sessions(date:NaiveDate,schedule:&MarketSchedule)->CoreResult<Vec<(Timestamp,Timestamp)>> {
    let mut out=Vec::new();for offset in -32..2 {for occurrence in occurrences_on(date_add(date,offset)?,schedule)?{if occurrence.trading_date==date{out.push((occurrence.start,occurrence.end));}}}out.sort_unstable();Ok(out)
}
/// Canonical minute facts remain stored even when the configured calendar has
/// no session for them. Derived periods admit only declared-session members.
pub fn belongs_to_schedule(at:Timestamp,schedule:Option<&MarketSchedule>)->CoreResult<bool> {
    if schedule.is_none_or(|s|!has_schedule(s)){return Ok(true);}
    Ok(session_occurrence(at,schedule)?.is_some())
}
/// Skip a closed gap without decoding every retained minute in that gap.
pub fn session_neighbors(at:Timestamp,schedule:&MarketSchedule)->CoreResult<(Option<Timestamp>,Option<Timestamp>)> {
    let zone=schedule.zone()?;let date=at.with_timezone(&zone).date_naive();let mut before=None;let mut after=None;
    let mut inspect=|start:Timestamp,end:Timestamp| {
        if end<=at{before=Some(before.map_or(end,|value:Timestamp|value.max(end)));}
        if start>at{after=Some(after.map_or(start,|value:Timestamp|value.min(start)));}
    };
    let mut anchors=std::collections::BTreeSet::from([date]);
    if let Some(authority)=&schedule.authority {
        for day in &authority.absolute_days {for session in &day.continuous_sessions {inspect(session.start,session.end);}}
        if let Some(exceptions)=&authority.date_exceptions {for year in &exceptions.verified_years {
            anchors.insert(date.clamp(date_at(*year,1,1)?,date_at(*year,12,31)?));
        }}
    }
    for anchor in anchors {for offset in -32..33 {for occurrence in occurrences_on(date_add(anchor,offset)?,schedule)?{inspect(occurrence.start,occurrence.end);}}}
    Ok((before,after))
}
/// Disjoint UTC intervals belonging to a schedule, clipped to [start,end).
/// Range-index queries use these intervals instead of scanning every minute.
pub fn session_ranges(start:Timestamp,end:Timestamp,schedule:Option<&MarketSchedule>)->CoreResult<Vec<(Timestamp,Timestamp)>> {
    require(start<=end,"inverted calendar input range")?;
    let Some(schedule)=schedule.filter(|s|has_schedule(s)) else{return Ok(vec![(start,end)]);};
    schedule.validate()?;let zone=schedule.zone()?;
    let mut date=date_add(start.with_timezone(&zone).date_naive(),-2)?;
    let last=end.with_timezone(&zone).date_naive();let mut ranges=Vec::new();
    while date<=last {
        for occurrence in occurrences_on(date,schedule)? {
            let lo=occurrence.start.max(start);let hi=occurrence.end.min(end);
            if lo<hi {ranges.push((lo,hi));}
        }date=date_add(date,1)?;
    }
    ranges.sort_unstable();let mut merged=Vec::<(Timestamp,Timestamp)>::new();
    for (lo,hi) in ranges {
        if let Some(last)=merged.last_mut(){if lo<=last.1 {last.1=last.1.max(hi);continue;}}
        merged.push((lo,hi));
    }Ok(merged)
}
fn trading_day_bounds(date:NaiveDate,schedule:&MarketSchedule)->CoreResult<Option<(Timestamp,Timestamp)>> {
    let occurrences=trading_day_sessions(date,schedule)?;
    Ok(occurrences.iter().map(|v|v.0).min().zip(occurrences.iter().map(|v|v.1).max()))
}
fn trading_period_bounds(
    start: NaiveDate,
    end: NaiveDate,
    schedule: Option<&MarketSchedule>,
) -> CoreResult<Option<(Timestamp, Timestamp)>> {
    let Some(schedule) = schedule.filter(|s| has_schedule(s)) else {
        return Ok(None);
    };
    let mut first = None;
    let mut last = None;
    for offset in 0..32.min((end - start).num_days()) {
        if first.is_none() {
            first = trading_day_bounds(date_add(start, offset)?, schedule)?;
        }
        if last.is_none() {
            last = trading_day_bounds(date_add(end, -offset - 1)?, schedule)?;
        }
        if let (Some(first), Some(last)) = (first, last) {
            return Ok(Some((first.0, last.1)));
        }
    }
    Ok(None)
}
fn calendar_bounds(date: NaiveDate, period: Period) -> CoreResult<(NaiveDate, NaiveDate)> {
    Ok(match period {
        Period::D1 => (date, date_add(date, 1)?),
        Period::W1 => {
            let start = date_add(date, -i64::from(date.weekday().num_days_from_monday()))?;
            (start, date_add(start, 7)?)
        }
        Period::Mo1 => (
            date_at(date.year(), date.month(), 1)?,
            date_at(
                date.year() + i32::from(date.month() == 12),
                date.month() % 12 + 1,
                1,
            )?,
        ),
        Period::Q1 => {
            let month = (date.month() - 1) / 3 * 3 + 1;
            let end = month + 3;
            (
                date_at(date.year(), month, 1)?,
                date_at(date.year() + i32::from(end > 12), (end - 1) % 12 + 1, 1)?,
            )
        }
        Period::Y1 => (date_at(date.year(), 1, 1)?, date_at(date.year() + 1, 1, 1)?),
        _ => return Err(CoreError("fixed period has no calendar bounds".into())),
    })
}
pub fn bucket_for(
    at: Timestamp,
    period: Period,
    schedule: Option<&MarketSchedule>,
) -> CoreResult<Bucket> {
    let occurrence = session_occurrence(at, schedule)?;
    let zone = schedule
        .map(MarketSchedule::zone)
        .transpose()?
        .unwrap_or(chrono_tz::UTC);
    if let Some(seconds) = period.seconds() {
        let (start, end) = if let Some(occurrence) = occurrence {
            let offset = (at - occurrence.start).num_seconds() / seconds;
            let start = occurrence.local_start + Duration::seconds(seconds * offset);
            let end = occurrence.local_end.min(start + Duration::seconds(seconds));
            (localize(zone, start)?, localize(zone, end)?)
        } else {
            let start = crate::reducer::floor_time(at, seconds)?;
            (start, start + Duration::seconds(seconds))
        };
        return Ok(Bucket {
            key: format!("fixed:{}", isoformat(start)),
            start,
            end,
            evidence_start: None,
            evidence_end: None,
        });
    }
    let date = occurrence.map_or_else(|| at.with_timezone(&zone).date_naive(), |v| v.trading_date);
    let (start_date, end_date) = calendar_bounds(date, period)?;
    let start = localize(zone, midnight(start_date))?;
    let mut end = localize(zone, midnight(end_date))?;
    let evidence = trading_period_bounds(start_date, end_date, schedule)?;
    if period == Period::D1 {
        if let Some(evidence) = evidence {
            end = evidence.1;
        }
    }
    let unit = match period {
        Period::D1 => "day",
        Period::W1 => "week",
        Period::Mo1 => "month",
        Period::Q1 => "quarter",
        Period::Y1 => "year",
        _ => unreachable!(),
    };
    Ok(Bucket {
        key: format!("calendar:{unit}:{start_date}"),
        start,
        end,
        evidence_start: Some(evidence.map_or(start, |v| v.0)),
        evidence_end: Some(evidence.map_or(end, |v| v.1)),
    })
}
pub fn previous_bucket(
    bucket: &Bucket,
    period: Period,
    schedule: Option<&MarketSchedule>,
) -> CoreResult<Bucket> {
    let boundary = bucket.input_start();
    let mut probe = boundary - Duration::nanoseconds(1);
    if let Some(schedule) = schedule.filter(|s| has_schedule(s)) {
        if session_occurrence(probe, Some(schedule))?.is_none() {
            if let Some(end)=session_neighbors(probe,schedule)?.0 {probe=end-Duration::nanoseconds(1);}
        }
    }
    let previous = bucket_for(probe, period, schedule)?;
    require(
        previous.start < bucket.start,
        "period Bar bucket cursor did not advance",
    )?;
    Ok(previous)
}

pub fn project_bars(
    rows: &[RealtimeBar],
    period: Period,
    schedule: Option<&MarketSchedule>,
    now: Timestamp,
) -> CoreResult<Vec<RealtimeBar>> {
    let mut sorted: Vec<_> = rows.iter().collect();
    sorted.sort_by_key(|bar| bar.open_time);
    let mut groups: BTreeMap<String, (Bucket, Vec<&RealtimeBar>)> = BTreeMap::new();
    for row in sorted {
        if !period.is_base() && !belongs_to_schedule(row.open_time,schedule)? {continue;}
        let bucket = bucket_for(row.open_time, period, schedule)?;
        groups
            .entry(bucket.key.clone())
            .or_insert_with(|| (bucket, Vec::new()))
            .1
            .push(row);
    }
    let mut result = Vec::new();
    for (_, (bucket, members)) in groups {
        result.push(project_bucket_members(&members, period, &bucket, now,calendar_bucket_verified(members[0].open_time,period,schedule)?,calendar_evidence_known_at(members[0].open_time,period,schedule)?)?);
    }
    result.sort_by_key(|bar| bar.open_time);
    Ok(result)
}
fn project_bucket_members(
    members: &[&RealtimeBar],
    period: Period,
    bucket: &Bucket,
    now: Timestamp,
    calendar_verified:bool,
    calendar_known_at:Option<Timestamp>,
) -> CoreResult<RealtimeBar> {
    let first = members
        .iter()
        .min_by_key(|bar| bar.open_time)
        .ok_or_else(|| CoreError("cannot project empty bucket".into()))?;
    let latest = members
        .iter()
        .max_by_key(|bar| (bar.open_time, bar.source.received_at))
        .unwrap();
    let all_final = members.iter().all(|bar| bar.state == BarState::Final);
    let component_finalized=members.iter().filter_map(|bar|bar.finalized_at).max();
    let component_clock_unknown=members.iter().any(|bar|bar.finalized_at.is_none() || bar.source.raw("finalization_time_unknown")==Some(&json!(true)));
    let received=members.iter().map(|bar|bar.source.received_at).max().unwrap();
    let accepted=members.iter().filter_map(|bar|bar.source.raw("capture_accepted_at_ns").and_then(|v|v.as_str()?.parse::<i64>().ok()).map(chrono::DateTime::from_timestamp_nanos)).max();
    let availability=component_finalized.into_iter().chain(calendar_known_at).max().map(|at|at.max(bucket.input_end()).max(received).max(accepted.unwrap_or(at)));
    let state = if calendar_verified && calendar_known_at.is_none_or(|at|at<=now) && all_final && now >= bucket.input_end() && received<=now && accepted.is_none_or(|at|at<=now) && availability.is_none_or(|at|at<=now) {
        BarState::Final
    } else if members
        .iter()
        .any(|bar| bar.state != BarState::ProvisionalQuote)
    {
        BarState::ProvisionalAuthoritative
    } else {
        BarState::ProvisionalQuote
    };
    let mut known_volume_sum=Decimal::ZERO;
    let mut known_volume_count=0u64;
    let mut revision=0u64;
    let mut component_count=0u64;
    let mut accepted_known_count=0u64;
    let mut source_components=vec![];
    for bar in members {
        let prefix=bar.source.raw("derivation").and_then(Value::as_str)==Some("period_prefix");
        let raw_u64=|key:&str|bar.source.raw(key).and_then(|v|v.as_u64().or_else(||v.as_str().and_then(|s|s.parse().ok())));
        let total=if prefix{raw_u64("component_count").unwrap_or(1)}else{1};
        let accepted_known=if prefix{raw_u64("accepted_clock_known_component_count").unwrap_or(0)}else{u64::from(bar.source.raw("capture_accepted_at_ns").and_then(|v|v.as_str()?.parse::<i64>().ok()).is_some())};
        require(accepted_known<=total,"invalid period-prefix clock coverage")?;
        accepted_known_count=accepted_known_count.checked_add(accepted_known).ok_or_else(||CoreError("accepted clock count exceeds integer range".into()))?;
        let known=if prefix{raw_u64("known_volume_count").unwrap_or(if bar.volume.is_some(){total}else{0})}else{u64::from(bar.volume.is_some())};
        require(known<=total,"invalid period-prefix volume coverage")?;
        let part_sum=if prefix{bar.source.raw("known_volume_sum").and_then(|v|v.as_str()).map(Decimal::from_str_exact).transpose().map_err(|e|CoreError(e.to_string()))?.or_else(||bar.volume.clone())}else{bar.volume.clone()}.unwrap_or(Decimal::ZERO);
        known_volume_sum=known_volume_sum.checked_add(part_sum.clone()).ok_or_else(||CoreError("volume exact arithmetic unavailable".into()))?;
        crate::source_volume::merge(&mut source_components,crate::source_volume::read(bar.source.raw_payload.as_ref().unwrap_or(&Value::Null),part_sum,known,total)?)?;
        known_volume_count=known_volume_count.checked_add(known).ok_or_else(||CoreError("known volume count exceeds integer range".into()))?;
        component_count=component_count.checked_add(total).ok_or_else(||CoreError("component count exceeds integer range".into()))?;
        revision=revision.checked_add(bar.revision).ok_or_else(||CoreError("period revision exceeds integer range".into()))?;
    }
    let volume=(known_volume_count==component_count).then(||known_volume_sum.clone());
    let mut raw=json!({"calendar_bucket_verified":calendar_verified,"derivation":"backend_period_projection","period_id":period.as_str(),"bucket_first_open_time":isoformat(first.open_time),"bucket_end":isoformat(bucket.input_end()),"component_count":component_count.to_string(),"known_volume_count":known_volume_count.to_string(),"known_volume_sum":known_volume_sum.to_string(),"component_finalized_at_ns":component_finalized.and_then(|v|v.timestamp_nanos_opt()).map(|v|v.to_string()),"derived_availability_lower_bound_ns":availability.and_then(|v|v.timestamp_nanos_opt()).map(|v|v.to_string()),"capture_accepted_at_ns":accepted.and_then(|v|v.timestamp_nanos_opt()).map(|v|v.to_string()),"accepted_clock_known_component_count":accepted_known_count.to_string(),"accepted_clock_all_components_known":accepted_known_count==component_count,"finalization_time_unknown":component_finalized.is_none(),"source_publication_time_unknown":true,"finalization_clock_policy":"derived-availability-max-required-calendar-and-component-clocks-v2"});
    raw["finalization_time_unknown"]=json!(component_clock_unknown);
    raw["calendar_evidence_known_at_ns"]=json!(calendar_known_at.and_then(|at|at.timestamp_nanos_opt()).map(|v|v.to_string()));
    raw["calendar_evidence_clock_policy"]=json!("captured-required-date-received-accepted-max-v1; static policy clock remains unknown");
    crate::source_volume::write(&mut raw,&source_components)?;
    Ok(RealtimeBar {
        instrument: first.instrument.clone(),
        interval_seconds: period
            .seconds()
            .unwrap_or((bucket.end - bucket.start).num_seconds()),
        open_time: bucket.start,
        open: first.open.clone(),
        high: members.iter().map(|bar| bar.high.clone()).max().unwrap(),
        low: members.iter().map(|bar| bar.low.clone()).min().unwrap(),
        close: latest.close.clone(),
        volume,
        source: SourceMetadata {
            provider: latest.source.provider.clone(),
            provider_symbol: latest.source.provider_symbol.clone(),
            observed_at: members
                .iter()
                .map(|bar| bar.source.observed_at)
                .max()
                .unwrap(),
            received_at: members
                .iter()
                .map(|bar| bar.source.received_at)
                .max()
                .unwrap(),
            raw_payload: Some(raw),
        },
        evidence_channel_id: latest.evidence_channel_id.clone(),
        state,
        revision,
        finalized_at: if state == BarState::Final {availability}else{None},
    })
}

type MinuteKey = (String, Instrument);
type LiveKey = (String, Instrument, Period);
#[derive(Debug, Default)]
pub struct LivePeriodProjector {
    minutes: BTreeMap<MinuteKey, BTreeMap<Timestamp, RealtimeBar>>,
    active: BTreeMap<LiveKey, (Bucket, BTreeMap<Timestamp, RealtimeBar>)>,
    verified: BTreeMap<LiveKey,bool>,
    calendar_known_at:BTreeMap<LiveKey,Option<Timestamp>>,
}
impl LivePeriodProjector {
    pub fn snapshot(&self) -> CoreResult<Value> {
        serde_json::to_value((self.minutes.iter().collect::<Vec<_>>(), self.active.iter().collect::<Vec<_>>(),self.verified.iter().collect::<Vec<_>>(),self.calendar_known_at.iter().collect::<Vec<_>>()))
            .map_err(|e| CoreError(e.to_string()))
    }
    pub fn restore(snapshot: Value) -> CoreResult<Self> {
        type Minutes = Vec<(MinuteKey, BTreeMap<Timestamp, RealtimeBar>)>;
        type Active = Vec<(LiveKey, (Bucket, BTreeMap<Timestamp, RealtimeBar>))>;
        let (minutes,active,verified,calendar_known_at):(Minutes,Active,Vec<(LiveKey,bool)>,Vec<(LiveKey,Option<Timestamp>)>)=match snapshot.as_array().map(Vec::len) {
            Some(2)=>{let(m,a):(Minutes,Active)=serde_json::from_value(snapshot).map_err(|e|CoreError(e.to_string()))?;(m,a,vec![],vec![])},
            Some(3)=>{let(m,a,v):(Minutes,Active,Vec<(LiveKey,bool)>)=serde_json::from_value(snapshot).map_err(|e|CoreError(e.to_string()))?;(m,a,v,vec![])},
            _=>serde_json::from_value(snapshot).map_err(|e|CoreError(e.to_string()))?,
        };
        let mut result = Self::default();
        for (key, rows) in minutes {
            for (at, bar) in &rows {
                bar.validate()?;
                require(*at == bar.open_time && bar.source.provider == key.0 && bar.instrument == key.1,
                    "checkpoint minute key differs from its fact")?;
            }
            require(!result.minutes.contains_key(&key), "duplicate checkpoint minute series")?;
            result.minutes.insert(key, rows);
        }
        for (key, (bucket, rows)) in active {
            for (at, bar) in &rows {
                bar.validate()?;
                require(*at == bar.open_time && bar.source.provider == key.0 && bar.instrument == key.1,
                    "checkpoint active key differs from its fact")?;
            }
            require(!result.active.contains_key(&key), "duplicate checkpoint active series")?;
            result.active.insert(key, (bucket, rows));
        }
        result.verified=verified.into_iter().collect();
        result.calendar_known_at=calendar_known_at.into_iter().collect();
        Ok(result)
    }
    pub fn new() -> Self {
        Self::default()
    }
    pub fn seed(&mut self, rows: &[RealtimeBar]) -> CoreResult<()> {
        for row in rows {
            self.accept(row.clone(), None, &[])?;
        }
        Ok(())
    }
    /// Restore a full database prefix plus current hot components before first delivery.
    pub fn prepare(
        &mut self,
        source: &str,
        instrument: &Instrument,
        period: Period,
        bucket: Bucket,
        rows: Vec<RealtimeBar>,
    ) {
        let key = (source.into(), instrument.clone(), period);
        if self
            .active
            .get(&key)
            .is_some_and(|(active, _)| active.start > bucket.start)
        {
            return;
        }
        let mut components: BTreeMap<_, _> =
            rows.into_iter().map(|bar| (bar.open_time, bar)).collect();
        if let Some(hot) = self.minutes.get(&(source.into(), instrument.clone())) {
            for (at, bar) in hot.range(bucket.input_start()..bucket.input_end()) {
                components.insert(*at, bar.clone());
            }
        }
        self.active.insert(key, (bucket, components));
    }
    /// A restored prefix must carry calendar proof; old checkpoints without
    /// that proof cannot confirm a derived bucket merely by containing rows.
    pub fn prepare_calendar(&mut self,source:&str,instrument:&Instrument,period:Period,bucket:Bucket,rows:Vec<RealtimeBar>,schedule:Option<&MarketSchedule>)->CoreResult<()> {
        let key=(source.into(),instrument.clone(),period);
        let verified=rows.first().map(|row|calendar_bucket_verified(row.open_time,period,schedule)).transpose()?.unwrap_or(false);
        let known=rows.first().map(|row|calendar_evidence_known_at(row.open_time,period,schedule)).transpose()?.flatten();
        let replaces=self.active.get(&key).is_none_or(|(active,_)|active.start<=bucket.start);
        self.prepare(source,instrument,period,bucket,rows);
        if replaces {self.verified.insert(key.clone(),verified);self.calendar_known_at.insert(key,known);}
        Ok(())
    }
    pub fn accept(
        &mut self,
        bar: RealtimeBar,
        schedule: Option<&MarketSchedule>,
        periods: &[Period],
    ) -> CoreResult<Vec<(Period, RealtimeBar)>> {
        if bar.interval_seconds != 60 {
            return Ok(vec![]);
        }
        bar.validate()?;
        let minute_key = (bar.source.provider.clone(), bar.instrument.clone());
        let minutes = self.minutes.entry(minute_key).or_default();
        if minutes.get(&bar.open_time).is_none_or(|current| {
            bar.revision > current.revision
                || (bar.revision == current.revision
                    && bar.source.received_at >= current.source.received_at)
        }) {
            minutes.insert(bar.open_time, bar.clone());
        }
        while minutes.len() > 720 {
            minutes.pop_first();
        }
        if !belongs_to_schedule(bar.open_time,schedule)? {return Ok(vec![]);}
        let mut result = Vec::new();
        for period in periods {
            require(
                !period.is_base(),
                "live derived period cannot be 1s, timeline or 1m",
            )?;
            let bucket = bucket_for(bar.open_time, *period, schedule)?;
            let key = (bar.source.provider.clone(), bar.instrument.clone(), *period);
            let calendar_verified=calendar_bucket_verified(bar.open_time,*period,schedule)?;
            let calendar_known=calendar_evidence_known_at(bar.open_time,*period,schedule)?;
            self.verified.insert(key.clone(),calendar_verified);
            self.calendar_known_at.insert(key.clone(),calendar_known);
            if self
                .active
                .get(&key)
                .is_some_and(|(active, _)| bucket.start < active.start)
            {
                continue;
            }
            if self
                .active
                .get(&key)
                .is_none_or(|(active, _)| active.key != bucket.key)
            {
                let mut components = BTreeMap::new();
                for (at, value) in minutes.iter() {
                    if bucket_for(*at, *period, schedule)?.key == bucket.key {
                        components.insert(*at, value.clone());
                    }
                }
                self.active
                    .insert(key.clone(), (bucket.clone(), components));
            }
            let (_, components) = self.active.get_mut(&key).unwrap();
            if components.get(&bar.open_time).is_some_and(|current| {
                bar.revision < current.revision
                    || (bar.revision == current.revision
                        && bar.source.received_at < current.source.received_at)
            }) {
                continue;
            }
            components.insert(bar.open_time, bar.clone());
            let refs: Vec<_> = components.values().collect();
            result.push((
                *period,
                project_bucket_members(&refs, *period, &bucket, bar.source.received_at,calendar_verified,calendar_known)?,
            ));
        }
        Ok(result)
    }
    pub fn current(
        &self,
        source: &str,
        instrument: &Instrument,
        period: Period,
        now: Timestamp,
    ) -> CoreResult<Option<RealtimeBar>> {
        self.active
            .get(&(source.into(), instrument.clone(), period))
            .map(|(bucket, components)| {
                project_bucket_members(
                    &components.values().collect::<Vec<_>>(),
                    period,
                    bucket,
                    now,
                    self.verified.get(&(source.into(),instrument.clone(),period)).copied().unwrap_or(false),
                    self.calendar_known_at.get(&(source.into(),instrument.clone(),period)).copied().flatten(),
                )
            })
            .transpose()
    }
}

/// Canonical ASCII JSON matches the Python schedule hashes already used by chart cursors.
pub fn canonical_json_ascii(value: &Value) -> String {
    let raw = serde_json::to_string(value).expect("JSON value serializes");
    let mut result = String::new();
    for ch in raw.chars() {
        if ch.is_ascii() {
            result.push(ch);
        } else {
            for unit in ch.encode_utf16(&mut [0; 2]).iter() {
                result.push_str(&format!("\\u{unit:04x}"));
            }
        }
    }
    result
}
pub fn schedule_version(schedule: Option<&MarketSchedule>) -> CoreResult<String> {
    let value = serde_json::to_value(schedule).map_err(|e| CoreError(e.to_string()))?;
    Ok(format!(
        "{:x}",
        Sha256::digest(canonical_json_ascii(&value).as_bytes())
    )[..16]
        .to_string())
}
pub fn materialization_version(schedule: Option<&MarketSchedule>) -> CoreResult<String> {
    let value = json!({"algorithm":"period-bars-v2","schedule":schedule});
    Ok(format!(
        "period-bars-v2:{}",
        &format!(
            "{:x}",
            Sha256::digest(canonical_json_ascii(&value).as_bytes())
        )[..20]
    ))
}

#[cfg(test)] mod date_authority_tests {
    use super::*;
    fn official()->MarketSchedule {
        let values:Value=serde_json::from_str(include_str!("../assets/schedules.json")).unwrap();
        let mut schedule:MarketSchedule=serde_json::from_value(values["shfe_metals"].clone()).unwrap();
        schedule.authority=Some(CalendarAuthority{date_exceptions:Some(serde_json::from_str(include_str!("../assets/shfe-date-exceptions.json")).unwrap()),absolute_days:vec![]});schedule
    }
    #[test]fn official_annual_dates_match_independent_eighteen_trading_dates_and_five_civil_edges(){
        let schedule=official();schedule.validate().unwrap();let expected:Value=serde_json::from_str(include_str!("../tests/fixtures/calendar/shfe-date-cases.json")).unwrap();
        for case in expected["cases"].as_array().unwrap() {
            let date=case["trading_date"].as_str().unwrap().parse().unwrap();
            let actual=trading_day_sessions(date,&schedule).unwrap();
            let expected=case["expected_continuous_sessions"].as_array().unwrap().iter().map(|r|(r["local_start"].as_str().unwrap().parse::<Timestamp>().unwrap(),r["local_end"].as_str().unwrap().parse::<Timestamp>().unwrap())).collect::<Vec<_>>();
            assert_eq!(actual,expected,"trading date {date}");
        }
        for case in expected["critical_negative_cases"].as_array().unwrap(){
            let at=case["civil_local_time"].as_str().unwrap().parse().unwrap();
            let continuous=case["expected"]=="continuous_incoming_night";
            assert_eq!(belongs_to_schedule(at,Some(&schedule)).unwrap(),continuous,"{case}");
            if continuous{assert_eq!(session_occurrence(at,Some(&schedule)).unwrap().unwrap().trading_date.to_string(),case["trading_date"].as_str().unwrap());}
            if case["expected"]=="calendar_year_not_verified_do_not_assume_open"{assert!(!calendar_instant_verified(at,Some(&schedule)).unwrap());}
        }
        for at in ["2025-12-31T21:00:00+08:00","2026-01-04T09:00:00+08:00"]{assert!(!belongs_to_schedule(at.parse().unwrap(),Some(&schedule)).unwrap());}
        assert_eq!(trading_day_sessions("2027-01-04".parse().unwrap(),&schedule).unwrap().len(),0);
        let saturday:Timestamp="2026-09-19T01:00:00+08:00".parse().unwrap();let bucket=bucket_for(saturday,Period::D1,Some(&schedule)).unwrap();assert_eq!(bucket.start.with_timezone(&chrono_tz::Asia::Shanghai).date_naive().to_string(),"2026-09-21");
    }
    #[test]fn exact_date_source_calendar_never_repeats_weekly_or_confirms_long_periods(){
        let calendar:Value=serde_json::from_str(include_str!("../assets/fuyao-calendar.json")).unwrap();let authority:CalendarAuthority=serde_json::from_value(calendar["fuyao:65:au2612"].clone()).unwrap();
        let schedule=MarketSchedule{time_zone:"Asia/Shanghai".into(),trading_day_rule:Some(TradingDayRule::Shfe),reference:None,sessions:vec![],authority:Some(authority)};
        let at:Timestamp="2026-09-30T09:01:00+08:00".parse().unwrap();assert!(belongs_to_schedule(at,Some(&schedule)).unwrap());assert!(!belongs_to_schedule(at-Duration::days(1),Some(&schedule)).unwrap());
        assert!(calendar_bucket_verified(at,Period::D1,Some(&schedule)).unwrap());
        for period in [Period::W1,Period::Mo1,Period::Q1,Period::Y1]{assert!(!calendar_bucket_verified(at,period,Some(&schedule)).unwrap());}
        assert!(!calendar_instant_verified(at-Duration::days(1),Some(&schedule)).unwrap());assert!(calendar_instant_verified("2026-09-30T12:00:00+08:00".parse().unwrap(),Some(&schedule)).unwrap());
        assert!(!belongs_to_schedule("2026-09-30T12:00:00+08:00".parse().unwrap(),Some(&schedule)).unwrap());
        assert_eq!(session_neighbors("2020-01-01T00:00:00Z".parse().unwrap(),&schedule).unwrap().1,Some(schedule.authority.as_ref().unwrap().absolute_days[0].continuous_sessions[0].start));
        assert_eq!(session_neighbors("2030-01-01T00:00:00Z".parse().unwrap(),&schedule).unwrap().0,Some(schedule.authority.as_ref().unwrap().absolute_days[0].continuous_sessions.last().unwrap().end));
        assert!(calendar_bucket_verified("2027-01-01T00:00:00Z".parse().unwrap(),Period::M1,Some(&schedule)).unwrap(),"calendar coverage does not alter original base fact finality");
    }
    #[test]fn late_calendar_known_clock_matches_hot_cold_and_checkpoint_and_preserves_unknown_component(){
        let calendar:Value=serde_json::from_str(include_str!("../assets/fuyao-calendar.json")).unwrap();
        let authority:CalendarAuthority=serde_json::from_value(calendar["fuyao:65:au2612"].clone()).unwrap();
        let schedule=MarketSchedule{time_zone:"Asia/Shanghai".into(),trading_day_rule:Some(TradingDayRule::Shfe),reference:None,sessions:vec![],authority:Some(authority)};
        let golden:Value=serde_json::from_str(include_str!("../tests/fixtures/core/golden.json")).unwrap();
        let mut bar:RealtimeBar=serde_json::from_value(golden["periods"][0]["rows"][0].clone()).unwrap();
        bar.open_time="2026-09-30T01:01:00Z".parse().unwrap();bar.interval_seconds=60;bar.source.observed_at=bar.open_time;bar.source.received_at=bar.open_time+Duration::minutes(1);bar.finalized_at=Some(bar.source.received_at);bar.source.raw_payload=None;bar.state=BarState::Final;
        let known=calendar_evidence_known_at(bar.open_time,Period::D1,Some(&schedule)).unwrap().unwrap();let after=known+Duration::nanoseconds(1);
        let cold=project_bars(&[bar.clone()],Period::D1,Some(&schedule),after).unwrap().remove(0);
        assert_eq!(cold.finalized_at,Some(known));assert_eq!(cold.source.raw("calendar_evidence_known_at_ns"),Some(&json!(known.timestamp_nanos_opt().unwrap().to_string())));
        let before=project_bars(&[bar.clone()],Period::D1,Some(&schedule),known-Duration::nanoseconds(1)).unwrap();assert_ne!(before[0].state,BarState::Final);
        let mut hot=LivePeriodProjector::new();hot.accept(bar.clone(),Some(&schedule),&[Period::D1]).unwrap();assert_eq!(hot.current(&bar.source.provider,&bar.instrument,Period::D1,after).unwrap().unwrap(),cold);
        let restored=LivePeriodProjector::restore(hot.snapshot().unwrap()).unwrap();assert_eq!(restored.current(&bar.source.provider,&bar.instrument,Period::D1,after).unwrap().unwrap(),cold);
        let mut unknown=bar.clone();unknown.open_time+=Duration::minutes(1);unknown.finalized_at=None;
        let bucket=bucket_for(bar.open_time,Period::D1,Some(&schedule)).unwrap();
        for members in [vec![&bar,&unknown],vec![&unknown,&bar]] {let value=project_bucket_members(&members,Period::D1,&bucket,after,true,Some(known)).unwrap();assert_eq!(value.finalized_at,Some(known));assert_eq!(value.source.raw("finalization_time_unknown"),Some(&json!(true)));}
        let all_unknown=project_bucket_members(&[&unknown],Period::D1,&bucket,after,true,None).unwrap();assert!(all_unknown.finalized_at.is_none());
    }
}
