# Collaboration data dictionary

This document lists fields only; it never contains review text or row-level identifiers.

## `data/amazon_reviews_2023/processed/review_level_base_w3_v1_4_0.parquet`

- Rows: 55,877
- Fields: 23
- Source phase: W4R
- Release status: `APPROVED_BY_PROJECT_OWNER`

| Field | Arrow type |
|---|---|
| `parent_asin` | `string` |
| `asin` | `string` |
| `source_domain` | `string` |
| `source_domains` | `list<element: string>` |
| `device_type` | `string` |
| `main_category` | `string` |
| `product_title` | `string` |
| `timestamp_ms` | `int64` |
| `review_datetime` | `timestamp[us, tz=UTC]` |
| `review_month` | `date32[day]` |
| `rating` | `double` |
| `verified_purchase` | `bool` |
| `helpful_vote` | `int64` |
| `review_title` | `string` |
| `review_body` | `string` |
| `review_text` | `string` |
| `language` | `string` |
| `language_detected_iso` | `string` |
| `language_confidence` | `float` |
| `user_id_hash` | `string` |
| `duplicate_key` | `string` |
| `source_row_number` | `int64` |
| `filter_version` | `string` |

## `data/amazon_reviews_2023/processed/target_products_w3_v1_4_0.parquet`

- Rows: 125
- Fields: 38
- Source phase: W3R-C
- Release status: `APPROVED_BY_PROJECT_OWNER`

| Field | Arrow type |
|---|---|
| `parent_asin` | `string` |
| `source_domains` | `list<element: string>` |
| `primary_source_domain` | `string` |
| `primary_source_row_number` | `int64` |
| `main_category` | `string` |
| `title` | `string` |
| `categories` | `string` |
| `features` | `string` |
| `description` | `string` |
| `store` | `string` |
| `details` | `string` |
| `price` | `string` |
| `average_rating` | `double` |
| `rating_number` | `int64` |
| `candidate_device_types` | `list<element: string>` |
| `eligible_device_types` | `list<element: string>` |
| `candidate_device_terms` | `list<element: string>` |
| `candidate_smart_terms` | `list<element: string>` |
| `matched_fields` | `list<element: string>` |
| `exclusion_reasons` | `list<element: string>` |
| `candidate_confidence` | `string` |
| `provisional_device_type` | `string` |
| `device_type` | `string` |
| `eligible_after_exclusions` | `bool` |
| `ambiguity_status` | `string` |
| `candidate_reason` | `string` |
| `title_only` | `bool` |
| `filter_version` | `string` |
| `candidate_source_record_count` | `int32` |
| `duplicate_resolution_rule` | `string` |
| `coalesced_fields` | `list<element: string>` |
| `content_fingerprint` | `string` |
| `core_nonempty_count` | `int16` |
| `identity_text_chars` | `int32` |
| `promotion_source` | `string` |
| `promotion_basis` | `string` |
| `human_review_status` | `string` |
| `previous_filter_version` | `string` |

## `data/amazon_reviews_2023/processed/annotation_labels_w5c_b_v1_0.parquet`

- Rows: 1,500
- Fields: 17
- Source phase: W5-C-B
- Release status: `APPROVED_BY_PROJECT_OWNER`

| Field | Arrow type |
|---|---|
| `blind_review_id` | `large_string` |
| `duplicate_key` | `large_string` |
| `parent_asin` | `large_string` |
| `device_type` | `large_string` |
| `review_datetime` | `timestamp[us, tz=UTC]` |
| `final_failure_binary` | `large_string` |
| `final_failure_type` | `large_string` |
| `final_severity` | `int8` |
| `final_persistence` | `int8` |
| `annotation_source` | `large_string` |
| `label_status` | `large_string` |
| `annotation_version` | `large_string` |
| `keyword_candidate_hit` | `bool` |
| `sampling_round` | `large_string` |
| `sampling_strategy` | `large_string` |
| `previous_annotation_source` | `large_string` |
| `previous_annotation_version` | `large_string` |

## `data/amazon_reviews_2023/processed/review_level_failure_predictions_w6a_v1_0.parquet`

- Rows: 55,877
- Fields: 11
- Source phase: W6-A
- Release status: `APPROVED_BY_PROJECT_OWNER`

| Field | Arrow type |
|---|---|
| `duplicate_key` | `string` |
| `parent_asin` | `string` |
| `device_type` | `string` |
| `source_domain` | `string` |
| `review_datetime` | `timestamp[us, tz=UTC]` |
| `review_month` | `date32[day]` |
| `failure_probability` | `double` |
| `failure_prediction` | `int8` |
| `model_version` | `string` |
| `product_filter_version` | `string` |
| `analysis_role` | `string` |

## `data/amazon_reviews_2023/processed/review_level_signal_components_w6b_v1_0.parquet`

- Rows: 55,877
- Fields: 27
- Source phase: W6-B
- Release status: `APPROVED_BY_PROJECT_OWNER`

| Field | Arrow type |
|---|---|
| `duplicate_key` | `string` |
| `parent_asin` | `string` |
| `device_type` | `string` |
| `source_domain` | `string` |
| `review_datetime` | `timestamp[us, tz=UTC]` |
| `review_month` | `date32[day]` |
| `analysis_role` | `string` |
| `failure_probability` | `double` |
| `failure_prediction` | `int8` |
| `severity_probability_ge2_given_failure` | `double` |
| `severity_probability_ge3_given_failure` | `double` |
| `expected_severity_given_failure` | `double` |
| `expected_severity_signal` | `double` |
| `persistence_probability_ge1_given_failure` | `double` |
| `persistence_probability_ge2_given_failure` | `double` |
| `expected_persistence_given_failure` | `double` |
| `expected_persistence_signal` | `double` |
| `sentiment_compound` | `double` |
| `sentiment_positive` | `double` |
| `sentiment_neutral` | `double` |
| `sentiment_negative` | `double` |
| `negative_sentiment_indicator` | `int8` |
| `failure_model_version` | `string` |
| `severity_model_version` | `string` |
| `persistence_model_version` | `string` |
| `sentiment_model_version` | `string` |
| `product_filter_version` | `string` |

## `data/amazon_reviews_2023/processed/product_month_signal_components_w6b_v1_0.parquet`

- Rows: 1,911
- Fields: 18
- Source phase: W6-B
- Release status: `APPROVED_BY_PROJECT_OWNER`

| Field | Arrow type |
|---|---|
| `parent_asin` | `string` |
| `review_month` | `date32[day]` |
| `device_type` | `string` |
| `analysis_role` | `string` |
| `n_reviews` | `int64` |
| `predicted_failure_count` | `int64` |
| `predicted_failure_share` | `double` |
| `mean_failure_probability` | `double` |
| `mean_expected_severity_signal` | `double` |
| `mean_expected_persistence_signal` | `double` |
| `mean_sentiment_compound` | `double` |
| `negative_sentiment_count` | `int64` |
| `negative_sentiment_share` | `double` |
| `failure_model_version` | `string` |
| `severity_model_version` | `string` |
| `persistence_model_version` | `string` |
| `sentiment_model_version` | `string` |
| `product_filter_version` | `string` |

## `data/amazon_reviews_2023/processed/review_level_engineering_index_w6c_v1_0.parquet`

- Rows: 55,877
- Fields: 24
- Source phase: W6-C
- Release status: `APPROVED_BY_PROJECT_OWNER`

| Field | Arrow type |
|---|---|
| `duplicate_key` | `large_string` |
| `parent_asin` | `large_string` |
| `device_type` | `large_string` |
| `source_domain` | `large_string` |
| `review_datetime` | `timestamp[us, tz=UTC]` |
| `review_month` | `date32[day]` |
| `analysis_role` | `large_string` |
| `failure_probability` | `double` |
| `severity_probability_ge2_given_failure` | `double` |
| `severity_probability_ge3_given_failure` | `double` |
| `expected_persistence_given_failure` | `double` |
| `normalized_persistence_given_failure` | `double` |
| `normalized_full_severity_exploratory` | `double` |
| `engineering_index_main` | `double` |
| `engineering_index_failure_only` | `double` |
| `engineering_index_equal_weight` | `double` |
| `engineering_index_failure_emphasis` | `double` |
| `engineering_index_full_severity_exploratory` | `double` |
| `failure_model_version` | `large_string` |
| `severity_model_version` | `large_string` |
| `persistence_model_version` | `large_string` |
| `sentiment_model_version` | `large_string` |
| `product_filter_version` | `large_string` |
| `engineering_index_version` | `large_string` |

## `data/amazon_reviews_2023/processed/product_month_engineering_index_w6c_v1_0.parquet`

- Rows: 1,911
- Fields: 26
- Source phase: W6-C
- Release status: `APPROVED_BY_PROJECT_OWNER`

| Field | Arrow type |
|---|---|
| `parent_asin` | `large_string` |
| `review_month` | `date32[day]` |
| `device_type` | `large_string` |
| `analysis_role` | `large_string` |
| `n_reviews` | `int64` |
| `mean_engineering_index_main` | `double` |
| `mean_engineering_index_failure_only` | `double` |
| `mean_engineering_index_equal_weight` | `double` |
| `mean_engineering_index_failure_emphasis` | `double` |
| `mean_engineering_index_full_severity_exploratory` | `double` |
| `predicted_failure_share` | `double` |
| `mean_failure_probability` | `double` |
| `mean_expected_severity_signal` | `double` |
| `mean_expected_persistence_signal` | `double` |
| `mean_sentiment_compound` | `double` |
| `negative_sentiment_share` | `double` |
| `failure_model_version` | `large_string` |
| `severity_model_version` | `large_string` |
| `persistence_model_version` | `large_string` |
| `sentiment_model_version` | `large_string` |
| `product_filter_version` | `large_string` |
| `rating_sum` | `double` |
| `low_star_count` | `int64` |
| `mean_rating` | `double` |
| `low_star_share` | `double` |
| `engineering_index_version` | `large_string` |

## `data/amazon_reviews_2023/processed/product_month_quality_targets_w6c_v1_0.parquet`

- Rows: 1,911
- Fields: 80
- Source phase: W6-C
- Release status: `APPROVED_BY_PROJECT_OWNER`

| Field | Arrow type |
|---|---|
| `parent_asin` | `large_string` |
| `review_month` | `date32[day]` |
| `device_type` | `large_string` |
| `n_reviews` | `int64` |
| `rating_sum` | `double` |
| `low_star_count` | `int64` |
| `mean_rating` | `double` |
| `low_star_share` | `double` |
| `origin_has_reviews` | `bool` |
| `analysis_role` | `large_string` |
| `historical_window_complete` | `bool` |
| `historical_n_reviews` | `int64` |
| `historical_rating_mean` | `double` |
| `historical_low_star_share` | `double` |
| `future_window_complete_h1` | `bool` |
| `future_n_reviews_h1` | `int64` |
| `future_rating_mean_h1` | `double` |
| `future_low_star_share_h1` | `double` |
| `future_window_complete_h2` | `bool` |
| `future_n_reviews_h2` | `int64` |
| `future_rating_mean_h2` | `double` |
| `future_low_star_share_h2` | `double` |
| `future_window_complete_h3` | `bool` |
| `future_n_reviews_h3` | `int64` |
| `future_rating_mean_h3` | `double` |
| `future_low_star_share_h3` | `double` |
| `target_definite_h1` | `bool` |
| `rating_deterioration_h1` | `int8` |
| `low_star_deterioration_h1` | `int8` |
| `quality_deterioration_h1` | `int8` |
| `support_main_counts_h1` | `bool` |
| `eligible_main_h1` | `bool` |
| `eligible_current_ge10_h1` | `bool` |
| `eligible_current_ge20_h1` | `bool` |
| `quality_deterioration_h1_r20_l05` | `int8` |
| `quality_deterioration_h1_r20_l10` | `int8` |
| `quality_deterioration_h1_r20_l15` | `int8` |
| `quality_deterioration_h1_r30_l05` | `int8` |
| `quality_deterioration_h1_r30_l10` | `int8` |
| `quality_deterioration_h1_r30_l15` | `int8` |
| `quality_deterioration_h1_r40_l05` | `int8` |
| `quality_deterioration_h1_r40_l10` | `int8` |
| `quality_deterioration_h1_r40_l15` | `int8` |
| `target_definite_h2` | `bool` |
| `rating_deterioration_h2` | `int8` |
| `low_star_deterioration_h2` | `int8` |
| `quality_deterioration_h2` | `int8` |
| `support_main_counts_h2` | `bool` |
| `eligible_main_h2` | `bool` |
| `eligible_current_ge10_h2` | `bool` |
| `eligible_current_ge20_h2` | `bool` |
| `quality_deterioration_h2_r20_l05` | `int8` |
| `quality_deterioration_h2_r20_l10` | `int8` |
| `quality_deterioration_h2_r20_l15` | `int8` |
| `quality_deterioration_h2_r30_l05` | `int8` |
| `quality_deterioration_h2_r30_l10` | `int8` |
| `quality_deterioration_h2_r30_l15` | `int8` |
| `quality_deterioration_h2_r40_l05` | `int8` |
| `quality_deterioration_h2_r40_l10` | `int8` |
| `quality_deterioration_h2_r40_l15` | `int8` |
| `target_definite_h3` | `bool` |
| `rating_deterioration_h3` | `int8` |
| `low_star_deterioration_h3` | `int8` |
| `quality_deterioration_h3` | `int8` |
| `support_main_counts_h3` | `bool` |
| `eligible_main_h3` | `bool` |
| `eligible_current_ge10_h3` | `bool` |
| `eligible_current_ge20_h3` | `bool` |
| `quality_deterioration_h3_r20_l05` | `int8` |
| `quality_deterioration_h3_r20_l10` | `int8` |
| `quality_deterioration_h3_r20_l15` | `int8` |
| `quality_deterioration_h3_r30_l05` | `int8` |
| `quality_deterioration_h3_r30_l10` | `int8` |
| `quality_deterioration_h3_r30_l15` | `int8` |
| `quality_deterioration_h3_r40_l05` | `int8` |
| `quality_deterioration_h3_r40_l10` | `int8` |
| `quality_deterioration_h3_r40_l15` | `int8` |
| `target_version` | `large_string` |
| `proposed_split_h3` | `large_string` |
| `split_version` | `large_string` |

## `data/amazon_reviews_2023/processed/product_month_analysis_panel_w6c_v1_0.parquet`

- Rows: 1,911
- Fields: 88
- Source phase: W6-C
- Release status: `APPROVED_BY_PROJECT_OWNER`

| Field | Arrow type |
|---|---|
| `parent_asin` | `large_string` |
| `review_month` | `date32[day]` |
| `device_type` | `large_string` |
| `analysis_role` | `large_string` |
| `feature_n_reviews` | `int64` |
| `feature_mean_rating` | `double` |
| `feature_low_star_share` | `double` |
| `feature_predicted_failure_share` | `double` |
| `feature_mean_failure_probability` | `double` |
| `feature_mean_expected_severity_signal` | `double` |
| `feature_mean_expected_persistence_signal` | `double` |
| `feature_mean_sentiment_compound` | `double` |
| `feature_negative_sentiment_share` | `double` |
| `feature_mean_engineering_index_main` | `double` |
| `feature_mean_engineering_index_failure_only` | `double` |
| `feature_mean_engineering_index_equal_weight` | `double` |
| `feature_mean_engineering_index_failure_emphasis` | `double` |
| `feature_mean_engineering_index_full_severity_exploratory` | `double` |
| `feature_historical_n_reviews` | `int64` |
| `feature_historical_rating_mean` | `double` |
| `feature_historical_low_star_share` | `double` |
| `target_future_window_complete_h1` | `bool` |
| `target_future_n_reviews_h1` | `int64` |
| `target_future_rating_mean_h1` | `double` |
| `target_future_low_star_share_h1` | `double` |
| `target_future_window_complete_h2` | `bool` |
| `target_future_n_reviews_h2` | `int64` |
| `target_future_rating_mean_h2` | `double` |
| `target_future_low_star_share_h2` | `double` |
| `target_future_window_complete_h3` | `bool` |
| `target_future_n_reviews_h3` | `int64` |
| `target_future_rating_mean_h3` | `double` |
| `target_future_low_star_share_h3` | `double` |
| `target_rating_deterioration_h1` | `int8` |
| `target_low_star_deterioration_h1` | `int8` |
| `target_quality_deterioration_h1` | `int8` |
| `target_quality_deterioration_h1_r20_l05` | `int8` |
| `target_quality_deterioration_h1_r20_l10` | `int8` |
| `target_quality_deterioration_h1_r20_l15` | `int8` |
| `target_quality_deterioration_h1_r30_l05` | `int8` |
| `target_quality_deterioration_h1_r30_l10` | `int8` |
| `target_quality_deterioration_h1_r30_l15` | `int8` |
| `target_quality_deterioration_h1_r40_l05` | `int8` |
| `target_quality_deterioration_h1_r40_l10` | `int8` |
| `target_quality_deterioration_h1_r40_l15` | `int8` |
| `target_rating_deterioration_h2` | `int8` |
| `target_low_star_deterioration_h2` | `int8` |
| `target_quality_deterioration_h2` | `int8` |
| `target_quality_deterioration_h2_r20_l05` | `int8` |
| `target_quality_deterioration_h2_r20_l10` | `int8` |
| `target_quality_deterioration_h2_r20_l15` | `int8` |
| `target_quality_deterioration_h2_r30_l05` | `int8` |
| `target_quality_deterioration_h2_r30_l10` | `int8` |
| `target_quality_deterioration_h2_r30_l15` | `int8` |
| `target_quality_deterioration_h2_r40_l05` | `int8` |
| `target_quality_deterioration_h2_r40_l10` | `int8` |
| `target_quality_deterioration_h2_r40_l15` | `int8` |
| `target_rating_deterioration_h3` | `int8` |
| `target_low_star_deterioration_h3` | `int8` |
| `target_quality_deterioration_h3` | `int8` |
| `target_quality_deterioration_h3_r20_l05` | `int8` |
| `target_quality_deterioration_h3_r20_l10` | `int8` |
| `target_quality_deterioration_h3_r20_l15` | `int8` |
| `target_quality_deterioration_h3_r30_l05` | `int8` |
| `target_quality_deterioration_h3_r30_l10` | `int8` |
| `target_quality_deterioration_h3_r30_l15` | `int8` |
| `target_quality_deterioration_h3_r40_l05` | `int8` |
| `target_quality_deterioration_h3_r40_l10` | `int8` |
| `target_quality_deterioration_h3_r40_l15` | `int8` |
| `historical_window_complete` | `bool` |
| `target_definite_h1` | `bool` |
| `support_main_counts_h1` | `bool` |
| `eligible_main_h1` | `bool` |
| `eligible_current_ge10_h1` | `bool` |
| `eligible_current_ge20_h1` | `bool` |
| `target_definite_h2` | `bool` |
| `support_main_counts_h2` | `bool` |
| `eligible_main_h2` | `bool` |
| `eligible_current_ge10_h2` | `bool` |
| `eligible_current_ge20_h2` | `bool` |
| `target_definite_h3` | `bool` |
| `support_main_counts_h3` | `bool` |
| `eligible_main_h3` | `bool` |
| `eligible_current_ge10_h3` | `bool` |
| `eligible_current_ge20_h3` | `bool` |
| `target_version` | `large_string` |
| `proposed_split_h3` | `large_string` |
| `split_version` | `large_string` |

## `data/amazon_reviews_2023/collaboration/review_level_base_w3_v1_4_0_collaboration_candidate.parquet`

This local derivative removes `user_id_hash` but is excluded as a duplicate because the formal frozen file was explicitly approved for release.

| Field | Arrow type |
|---|---|
| `parent_asin` | `string` |
| `asin` | `string` |
| `source_domain` | `string` |
| `source_domains` | `list<element: string>` |
| `device_type` | `string` |
| `main_category` | `string` |
| `product_title` | `string` |
| `timestamp_ms` | `int64` |
| `review_datetime` | `timestamp[us, tz=UTC]` |
| `review_month` | `date32[day]` |
| `rating` | `double` |
| `verified_purchase` | `bool` |
| `helpful_vote` | `int64` |
| `review_title` | `string` |
| `review_body` | `string` |
| `review_text` | `string` |
| `language` | `string` |
| `language_detected_iso` | `string` |
| `language_confidence` | `float` |
| `duplicate_key` | `string` |
| `source_row_number` | `int64` |
| `filter_version` | `string` |
