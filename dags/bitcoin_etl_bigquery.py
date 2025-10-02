from __future__ import annotations
from airflow.decorators import dag, task
from airflow.operators.python import get_current_context
from datetime import timedelta
import pendulum
import requests
import pandas as pd
from airflow.providers.google.cloud.hooks.bigquery import BigQueryHook

# ====== CONFIG ======
GCP_PROJECT = "mbaenap2025"  # e.g., "my-gcp-project"
BQ_DATASET = "crypto2"  # e.g., "crypto"
BQ_TABLE = "bitcoin_history_hourly"  # e.g., "bitcoin_history_hourly"
BQ_LOCATION = "US"  # dataset location: "US" or "EU"
GCP_CONN_ID = "google_cloud_default"  # Airflow connection with a SA that can write to BQ
# ====================

DEFAULT_ARGS = {
    "email_on_failure": True,
    "owner": "José Lobo",
}

@task
def fetch_and_to_gbq():
    """
    Fetch hourly BTC data for the past 6 months using CoinGecko and load it to BigQuery
    via pandas.DataFrame.to_gbq()
    """
    ctx = get_current_context()
    # 6-month window: [data_interval_start - 6 months, data_interval_start)
    end_time = ctx["data_interval_start"]
    start_time = end_time - timedelta(days=180)  # Approx 6 months
    print(f"[UTC] target window: {start_time} -> {end_time}")
    start_s = int(start_time.timestamp())  # CoinGecko expects seconds
    end_s = int(end_time.timestamp())
    url = "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart/range"
    params = {"vs_currency": "usd", "from": start_s, "to": end_s}
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    payload = r.json()
    prices = payload.get("prices", [])
    caps = payload.get("market_caps", [])
    vols = payload.get("total_volumes", [])
    if not prices:
        print("No data returned for the specified window.")
        return
    # Build a tidy DataFrame: time (UTC), price_usd, market_cap_usd, volume_usd
    df_p = pd.DataFrame(prices, columns=["time_ms", "price_usd"])
    df_c = pd.DataFrame(caps, columns=["time_ms", "market_cap_usd"])
    df_v = pd.DataFrame(vols, columns=["time_ms", "volume_usd"])
    df = df_p.merge(df_c, on="time_ms", how="outer").merge(df_v, on="time_ms", how="outer")
    df["time"] = pd.to_datetime(df["time_ms"], unit="ms", utc=True)
    df.drop(columns=["time_ms"], inplace=True)
    df.sort_values("time", inplace=True)
    # Preview in logs
    print(df.head(10).to_string())
    # -------- Load to BigQuery using pandas-gbq --------
    bq_hook = BigQueryHook(gcp_conn_id=GCP_CONN_ID, location=BQ_LOCATION, use_legacy_sql=False)
    credentials = bq_hook.get_credentials()
    print(f"cred: {credentials}")
    destination_table = f"{BQ_DATASET}.{BQ_TABLE}"
    # Optional explicit schema (helps first-time table creation)
    table_schema = [
        {"name": "time", "type": "TIMESTAMP"},
        {"name": "price_usd", "type": "FLOAT"},
        {"name": "market_cap_usd", "type": "FLOAT"},
        {"name": "volume_usd", "type": "FLOAT"},
    ]
    # Ensure 'time' is a column (not index)
    if df.index.name == "time":
        df = df.reset_index()
    # pandas-gbq: append rows; if table doesn't exist, it's created with schema
    df.to_gbq(
        destination_table=destination_table,
        project_id=GCP_PROJECT,
        if_exists="append",  # or "replace" / "fail"
        credentials=credentials,
        table_schema=table_schema,
        location=BQ_LOCATION,
        progress_bar=False,
    )
    print(f"Loaded {len(df)} rows to {GCP_PROJECT}.{destination_table} (location={BQ_LOCATION}).")

@dag(
    dag_id="fetch_bitcoin_data_hourly",
    default_args=DEFAULT_ARGS,
    schedule="0 0 * * *",  # daily at 00:00 UTC
    start_date=pendulum.datetime(2025, 9, 17, tz="UTC"),
    catchup=True,
    max_active_runs=1,  # limit concurrent backfills if desired
    owner_links={
        "José Lobo": "mailto:loboferreira.adv@gmail.com"
       
    },
    tags=["bitcoin", "etl", "coingecko", "bigquery", "pandas-gbq"],
)
def bitcoin_etl_bigquery():
    fetch_and_to_gbq()

dag = bitcoin_etl_bigquery()
