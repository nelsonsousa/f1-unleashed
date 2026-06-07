from sqlalchemy import Column, Integer, String, DateTime
from app.database import Base


class Race(Base):
    __tablename__ = "races"

    id = Column(Integer, primary_key=True, index=True)
    year = Column(Integer, index=True)
    round = Column(Integer)
    name = Column(String, index=True)
    circuit = Column(String)
    country = Column(String)
    date = Column(DateTime)
