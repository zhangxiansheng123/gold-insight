from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Gold Insight"
    app_host: str = "127.0.0.1"
    app_port: int = 8765

    # MySQL
    mysql_host: str = "localhost"
    mysql_port: int = 3306
    mysql_user: str = "root"
    mysql_password: str = "123456"
    mysql_database: str = "gold_insight"

    # 也可直接覆盖完整 URL：DATABASE_URL=mysql+pymysql://...
    database_url: str | None = None

    # 京东金融 · 浙商积存金
    jd_price_url: str = "https://api.jdjygold.com/gw2/generic/jrm/h5/m/stdLatestPrice"
    zheshang_sku: str = "1961543816"

    # 伦敦金现货（优先准确现货，不再用 GC=F 期货当盘口）
    london_spot_url: str = "https://api.goldprice.dev/v1/prices"
    london_spot_symbol: str = "XAU-USD-SPOT"
    london_spot_fallback_url: str = "https://api.gold-api.com/price/XAU"
    # 历史日线仍用 Yahoo 期货（仅作走势训练；当日收盘会被现货校准）
    london_yahoo_symbol: str = "GC=F"
    london_yahoo_spot: str = "GC=F"

    # 盎司→克
    troy_oz_to_gram: float = 31.1034768

    # 上海黄金交易所 · 黄金9999（东财），作积存金人民币日线骨架
    au9999_eastmoney_secid: str = "118.AU9999"

    # 预测带宽：残差 z 分位 + ATR 放大 + 价格比例地板（约覆盖日常波动）
    forecast_band_z: float = 2.0  # ~95% 正态分位（原 1.64 过紧）
    forecast_band_atr_mult: float = 1.2
    forecast_band_floor_pct: float = 0.012  # 至少约 ±1.2% * √step

    # 定时采集（分钟）
    quote_interval_minutes: int = 5
    history_sync_hours: int = 6
    # 自动重算预测（小时）
    forecast_interval_hours: int = 3

    # 预测默认回看天数
    lookback_days: int = 365

    def get_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return (
            f"mysql+pymysql://{self.mysql_user}:{self.mysql_password}"
            f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_database}"
            f"?charset=utf8mb4"
        )


settings = Settings()
