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

    # 伦敦金历史（COMEX 黄金期货近似）
    london_yahoo_symbol: str = "GC=F"
    # 伦敦现货备用
    london_yahoo_spot: str = "XAUUSD=X"

    # 盎司→克
    troy_oz_to_gram: float = 31.1034768

    # 定时采集（分钟）
    quote_interval_minutes: int = 5
    history_sync_hours: int = 6

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
