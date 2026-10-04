use super::*;
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

const SOURCE_PERIOD_FIXTURE: &str = include_str!(concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/tests/fixtures/source-period-min5-v1.json"
));

fn fixture() -> Value {
    serde_json::from_str(SOURCE_PERIOD_FIXTURE).expect("source period fixture is valid JSON")
}

fn fixture_bodies(fixture: &Value) -> &[Value] {
    fixture["bodies"]
        .as_array()
        .expect("fixture bodies are an array")
}

fn fixture_samples(fixture: &Value) -> &[Value] {
    fixture["samples"]
        .as_array()
        .expect("fixture samples are an array")
}

fn exact_feed(catalog: &crate::catalog::Catalog, market: &str, code: &str) -> PublicFeed {
    let matches = catalog
        .items
        .iter()
        .filter_map(|definition| definition.public_feed.as_ref())
        .filter(|feed| feed.market == market && feed.code == code)
        .collect::<Vec<_>>();
    assert_eq!(matches.len(), 1, "catalog mapping for {market}:{code}");
    matches[0].clone()
}

fn original_body(body: &Value) -> Value {
    serde_json::from_str(body["body_text"].as_str().expect("body text"))
        .expect("embedded original response body is valid JSON")
}

fn sha256(bytes: &[u8]) -> String {
    hex::encode(Sha256::digest(bytes))
}

fn single_row_payload(feed: &PublicFeed, fields: Value, row: Value) -> Value {
    json!({
        "status_code": 0,
        "data": {
            "quote_data": [{
                "market": feed.market,
                "code": feed.code,
                "data_fields": fields,
                "value": [row],
            }],
            "fail_params": null,
        },
        "status_msg": "ok",
    })
}

fn source_rows(result: &Value) -> &[Value] {
    result["rows"].as_array().expect("parsed source rows")
}

#[test]
fn retained_original_bodies_and_catalog_requests_replay_sixteen_exact_rows() {
    let fixture = fixture();
    assert_eq!(fixture["schema"], "tracefang-fuyao-source-period-min5-tests-v1");
    assert_eq!(fixture["fixture_role"], "test_only_derived_fixture_with_original_response_bytes");
    assert_eq!(fixture_samples(&fixture).len(), 16);
    assert_eq!(fixture_bodies(&fixture).len(), 19);

    let catalog = crate::catalog::Catalog::embedded().expect("embedded catalog");
    for body in fixture_bodies(&fixture) {
        let body_text = body["body_text"].as_str().expect("body text");
        let receipt = &body["receipt"];
        assert_eq!(sha256(body_text.as_bytes()), body["body_sha256"]);
        assert_eq!(body["body_sha256"], receipt["sha256"]);
        assert_eq!(receipt["status"], 200);
        assert_eq!(receipt["bytes"].as_u64(), Some(body_text.len() as u64));

        let market = body["market"].as_str().expect("market");
        let code = body["code"].as_str().expect("code");
        let feed = exact_feed(&catalog, market, code);
        let request = five_minute_request(&feed, 100, 0);
        assert_eq!(request, receipt["request"], "exact catalog request for {market}:{code}");

        let payload = original_body(body);
        assert_eq!(payload["status_code"], 0);
        assert_eq!(payload["data"]["fail_params"], Value::Null);
        let quote_data = payload["data"]["quote_data"].as_array().unwrap();
        let expected_rows = body["row_count"].as_u64().unwrap() as usize;
        if expected_rows == 0 {
            assert!(quote_data.is_empty());
        } else {
            assert_eq!(quote_data.len(), 1);
            let section = &quote_data[0];
            assert_eq!(section["market"], body["market"]);
            assert_eq!(section["code"], body["code"]);
            assert_eq!(section["value"].as_array().unwrap().len(), expected_rows);
        }
    }

    for sample in fixture_samples(&fixture) {
        let body = fixture_bodies(&fixture)
            .iter()
            .find(|body| body["body_sha256"] == sample["body_sha256"])
            .expect("sample points to a frozen original body");
        let market = sample["market"].as_str().unwrap();
        let code = sample["code"].as_str().unwrap();
        assert_eq!(body["market"], sample["market"]);
        assert_eq!(body["code"], sample["code"]);

        let payload = original_body(body);
        let sections = payload["data"]["quote_data"].as_array().unwrap();
        let matching = sections
            .iter()
            .filter(|section| section["market"] == market && section["code"] == code)
            .collect::<Vec<_>>();
        assert_eq!(matching.len(), 1, "response section binds exact {market}:{code}");
        let section = matching[0];
        let row_index = sample["row_index"].as_u64().unwrap() as usize;
        let raw_row = &section["value"][row_index];
        let data_fields = section["data_fields"].as_array().unwrap();
        let raw_fields = raw_row.as_array().unwrap();
        let mut mapped_fields = Map::new();
        for (field, value) in data_fields.iter().zip(raw_fields) {
            mapped_fields.insert(field.as_str().unwrap().to_owned(), value.clone());
        }
        assert_eq!(exact(&Value::Object(mapped_fields)), sample["source_fields"]);
        assert_eq!(sample["source_fields"]["1"], sample["source_label"]);
        for (output_key, source_key) in [
            ("open", "7"),
            ("high", "8"),
            ("low", "9"),
            ("close", "11"),
            ("volume", "13"),
            ("turnover", "19"),
        ] {
            assert_eq!(sample[output_key], sample["source_fields"][source_key]);
        }

        let feed = exact_feed(&catalog, market, code);
        let request = five_minute_request(&feed, 100, 0);
        let parsed = parse_reported_five_minutes(&payload, &feed, &request).unwrap();
        assert_eq!(parsed["source_response_state"], "rows");
        let output_row = &source_rows(&parsed)[row_index];
        assert_eq!(output_row["row_index"].as_u64(), Some(row_index as u64));
        assert_eq!(output_row["source_label"], sample["source_label"]);
        assert_eq!(output_row["source_fields"], sample["source_fields"]);
        assert_eq!(output_row["field_presence"], json!({
            "1": true, "7": true, "8": true, "9": true, "11": true, "13": true, "19": true
        }));
        for key in ["open", "high", "low", "close", "volume", "turnover"] {
            assert_eq!(output_row[key], sample[key], "{market}:{code} row {row_index} {key}");
        }
        for key in [
            "observed_at",
            "published_at",
            "support_interval_start",
            "support_interval_end",
            "finalized_at",
            "source_reported_change",
            "source_reported_change_percent",
            "source_reported_price_change_basis",
        ] {
            assert!(output_row[key].is_null(), "{market}:{code} row {row_index} {key} stays unknown");
        }
        assert_eq!(output_row["finality"], "unknown");
        assert_eq!(output_row["canonical_bar"], false);
    }

}

#[test]
fn five_real_successful_empty_responses_stay_empty_and_fail_params_are_rejected() {
    let fixture = fixture();
    let catalog = crate::catalog::Catalog::embedded().expect("embedded catalog");
    let empty_bodies = fixture_bodies(&fixture)
        .iter()
        .filter(|body| body["row_count"] == 0)
        .collect::<Vec<_>>();
    assert_eq!(empty_bodies.len(), 5);

    for body in empty_bodies {
        let feed = exact_feed(
            &catalog,
            body["market"].as_str().unwrap(),
            body["code"].as_str().unwrap(),
        );
        let request = body["receipt"]["request"].clone();
        let mut payload = original_body(body);
        let parsed = parse_reported_five_minutes(&payload, &feed, &request).unwrap();
        assert_eq!(parsed["source_response_state"], "empty");
        assert!(source_rows(&parsed).is_empty());
        assert!(parsed["source_delay"].is_null());

        payload["data"]["fail_params"] = json!([{"market": feed.market, "code": feed.code}]);
        assert!(parse_reported_five_minutes(&payload, &feed, &request).is_err());
    }
}

#[test]
fn request_and_response_identity_and_period_must_match_the_catalog_feed() {
    let fixture = fixture();
    let body = fixture_bodies(&fixture)
        .iter()
        .find(|body| body["row_count"] == 100)
        .unwrap();
    let catalog = crate::catalog::Catalog::embedded().expect("embedded catalog");
    let feed = exact_feed(&catalog, body["market"].as_str().unwrap(), body["code"].as_str().unwrap());
    let payload = original_body(body);
    let request = five_minute_request(&feed, 100, 0);

    let mut wrong_period = request.clone();
    wrong_period["time_period"] = json!("min_1");
    assert!(parse_reported_five_minutes(&payload, &feed, &wrong_period).is_err());

    let mut wrong_request_identity = request.clone();
    wrong_request_identity["code_list"][0]["market"] = json!("wrong-market");
    assert!(parse_reported_five_minutes(&payload, &feed, &wrong_request_identity).is_err());

    let mut wrong_response_identity = payload.clone();
    wrong_response_identity["data"]["quote_data"][0]["code"] = json!("wrong-contract");
    assert!(parse_reported_five_minutes(&wrong_response_identity, &feed, &request).is_err());
}

#[test]
fn zero_null_and_missing_values_remain_distinct() {
    let frozen=fixture();
    let actual=fixture_bodies(&frozen).iter().find(|body|body["market"]=="65"&&body["code"]=="ad2611").unwrap();
    let actual_catalog=crate::catalog::Catalog::embedded().unwrap();let actual_feed=exact_feed(&actual_catalog,"65","ad2611");
    let original=original_body(actual);assert_eq!(exact(&original["data"]["quote_data"][0]["value"][45][5]),json!("0"));
    let preserved=parse_reported_five_minutes(&original,&actual_feed,&actual["receipt"]["request"]).unwrap();
    assert_eq!(source_rows(&preserved).len(),100);assert_eq!(preserved["rows"][45]["volume"],"0");assert_eq!(preserved["rows"][45]["field_presence"]["13"],true);
    let catalog = crate::catalog::Catalog::embedded().expect("embedded catalog");
    let feed = exact_feed(&catalog, "129", "IC2612");
    let request = five_minute_request(&feed, 1, 0);
    let fields = json!(["1", "7", "8", "9", "11", "13", "19"]);
    let label = "1790662200000";

    let zero = single_row_payload(
        &feed,
        fields.clone(),
        json!([label, "0", "0", "0", "0", "0", "0"]),
    );
    let zero_result = parse_reported_five_minutes(&zero, &feed, &request).unwrap();
    let zero_row = &source_rows(&zero_result)[0];
    assert_eq!(zero_row["open"], "0");
    assert_eq!(zero_row["volume"], "0");
    assert_eq!(zero_row["source_fields"]["13"], "0");
    assert_eq!(zero_row["field_presence"]["13"], true);

    let null = single_row_payload(
        &feed,
        fields,
        json!([label, "1", "2", "0.5", "1.5", null, "10"]),
    );
    let null_result = parse_reported_five_minutes(&null, &feed, &request).unwrap();
    let null_row = &source_rows(&null_result)[0];
    assert!(null_row["volume"].is_null());
    assert!(null_row["source_fields"]["13"].is_null());
    assert_eq!(null_row["field_presence"]["13"], true);

    let missing_fields = json!(["1", "7", "8", "9", "11", "19"]);
    let missing = single_row_payload(
        &feed,
        missing_fields,
        json!([label, "1", "2", "0.5", "1.5", "10"]),
    );
    let missing_result = parse_reported_five_minutes(&missing, &feed, &request).unwrap();
    let missing_row = &source_rows(&missing_result)[0];
    assert!(missing_row["volume"].is_null());
    assert!(missing_row["source_fields"]["13"].is_null());
    assert_eq!(missing_row["field_presence"]["13"], false);
}

#[test]
fn duplicate_field_names_and_wrong_row_width_are_rejected() {
    let catalog = crate::catalog::Catalog::embedded().expect("embedded catalog");
    let feed = exact_feed(&catalog, "129", "IC2612");
    let request = five_minute_request(&feed, 1, 0);
    let fields = json!(["1", "7", "8", "9", "11", "13", "19"]);
    let row = json!(["1790662200000", "1", "2", "0.5", "1.5", "0", "10"]);
    let payload = single_row_payload(&feed, fields, row);

    let mut duplicate_field = payload.clone();
    duplicate_field["data"]["quote_data"][0]["data_fields"] =
        json!(["1", "7", "7", "9", "11", "13", "19"]);
    assert!(parse_reported_five_minutes(&duplicate_field, &feed, &request).is_err());

    let mut wrong_width = payload;
    wrong_width["data"]["quote_data"][0]["value"][0]
        .as_array_mut()
        .unwrap()
        .pop();
    assert!(parse_reported_five_minutes(&wrong_width, &feed, &request).is_err());
}

#[test]
fn request_count_accepts_one_through_one_hundred_and_rejects_out_of_bounds_or_overfull_body() {
    let catalog = crate::catalog::Catalog::embedded().expect("embedded catalog");
    let feed = exact_feed(&catalog, "129", "IC2612");
    let empty_payload = json!({
        "status_code": 0,
        "data": {"quote_data": [], "fail_params": null},
        "status_msg": "ok",
    });

    for count in 1..=100 {
        let request = five_minute_request(&feed, count, 0);
        let parsed = parse_reported_five_minutes(&empty_payload, &feed, &request).unwrap();
        assert_eq!(parsed["source_response_state"], "empty");
        assert!(source_rows(&parsed).is_empty());
    }
    for count in [0, 101] {
        let request = five_minute_request(&feed, count, 0);
        assert!(parse_reported_five_minutes(&empty_payload, &feed, &request).is_err());
    }

    let fixture = fixture();
    let body = fixture_bodies(&fixture)
        .iter()
        .find(|body| body["market"] == "129" && body["code"] == "IC2612")
        .cloned()
        .unwrap();
    let payload = original_body(&body);
    let too_small = five_minute_request(&feed, 99, 0);
    assert!(parse_reported_five_minutes(&payload, &feed, &too_small).is_err());
}
