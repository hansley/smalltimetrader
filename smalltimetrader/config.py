from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Coin(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset: str = Field(pattern=r"^[A-Z0-9]{2,12}$")
    binance: str = Field(pattern=r"^[A-Z0-9]{2,12}USDT$")
    coinbase: str = Field(pattern=r"^[A-Z0-9]{2,12}-USD$")
    kraken: str = Field(pattern=r"^[A-Z0-9]{2,16}USD$")

    @model_validator(mode="after")
    def mapping(self):
        if self.binance != self.asset + "USDT" or self.coinbase != self.asset + "-USD":
            raise ValueError("Asset and Binance/Coinbase product must match")
        if self.kraken != ({"BTC": "XBT"}.get(self.asset, self.asset) + "USD"):
            raise ValueError("Kraken symbol must match asset (BTC maps to XBT)")
        return self


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    coins: list[Coin] = Field(default_factory=lambda: [Coin(asset="BTC", binance="BTCUSDT", coinbase="BTC-USD", kraken="XBTUSD"), Coin(asset="ETH", binance="ETHUSDT", coinbase="ETH-USD", kraken="ETHUSD")], min_length=1, max_length=30)
    interval_seconds: int = Field(default=600, ge=60, le=86400)
    window_seconds: int = Field(default=600, ge=60, le=86400)
    baseline_tolerance_seconds: int = Field(default=45, ge=1, le=120)
    max_age_seconds: int = Field(default=60, ge=5, le=120)
    max_disagreement_pct: float = Field(default=0.75, gt=0, le=5)
    alert_pct: float = Field(default=1, gt=0, le=100)
    trade_pct: float = Field(default=2, gt=0, le=100)
    cooldown_seconds: int = Field(default=1800, ge=60, le=86400)
    strategy: Literal["momentum", "reversal"] = "momentum"
    mode: Literal["paper", "testnet", "live"] = "paper"
    paused: bool = True
    order_usdt: float = Field(default=25, ge=5, le=10000)
    max_exposure_usdt: float = Field(default=100, ge=5, le=100000)
    daily_loss_usdt: float = Field(default=10, gt=0, le=10000)
    max_daily_turnover_usdt: float = Field(default=200, ge=5, le=100000)
    max_slippage_pct: float = Field(default=0.2, gt=0, le=1)
    max_spread_pct: float = Field(default=0.3, gt=0, le=2)
    stop_loss_pct: float = Field(default=3, gt=0, le=100)
    take_profit_pct: float = Field(default=6, gt=0, le=1000)
    paper_start_usdt: float = Field(default=1000, ge=10, le=1000000)
    paper_fee_pct: float = Field(default=0.1, ge=0, le=1)
    whatsapp_enabled: bool = False
    whatsapp_to: str = Field(default="", pattern=r"^(\+[1-9][0-9]{6,14})?$")

    @model_validator(mode="after")
    def valid(self):
        if len({c.asset for c in self.coins}) != len(self.coins):
            raise ValueError("Duplicate assets")
        if self.trade_pct <= self.alert_pct:
            raise ValueError("Trade threshold must exceed alert threshold")
        if self.order_usdt > self.max_exposure_usdt:
            raise ValueError("Order exceeds exposure limit")
        if self.whatsapp_enabled and not self.whatsapp_to:
            raise ValueError("WhatsApp recipient required")
        if self.window_seconds % self.interval_seconds != 0:
            raise ValueError("Comparison window must be a multiple of check interval")
        if self.baseline_tolerance_seconds >= self.window_seconds/2:
            raise ValueError("Baseline tolerance must be less than half the window")
        return self
