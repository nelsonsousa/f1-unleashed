from pydantic import BaseModel
from typing import Optional


class LapData(BaseModel):
    lap_number: int
    driver: str
    lap_time: Optional[str] = None
    sector1: Optional[str] = None
    sector2: Optional[str] = None
    sector3: Optional[str] = None
    compound: Optional[str] = None
    tyre_life: Optional[int] = None


class TelemetryPoint(BaseModel):
    time: float
    distance: float
    speed: float
    throttle: float
    brake: bool
    gear: int
    rpm: Optional[int] = None
    drs: Optional[int] = None


class DriverResult(BaseModel):
    position: Optional[int]
    driver_number: str
    driver: str
    team: str
    points: float
    status: str


class CircuitInfo(BaseModel):
    name: str
    circuit: str
    country: str
    date: str
    round: int


class DriverComparison(BaseModel):
    abbreviation: str
    lap_time: str
    sector1: str
    sector2: str
    sector3: str


class ComparisonResult(BaseModel):
    driver1: DriverComparison
    driver2: DriverComparison
    delta: str


class WeatherData(BaseModel):
    time: str
    air_temp: float
    track_temp: float
    humidity: float
    pressure: float
    wind_speed: float
    wind_direction: int
    rainfall: bool


class SessionInfo(BaseModel):
    year: int
    race: str
    session_type: str
    circuit: CircuitInfo
