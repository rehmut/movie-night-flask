from __future__ import annotations

from datetime import datetime
from typing import Optional

from flask_sqlalchemy import SQLAlchemy


db = SQLAlchemy()


class Event(db.Model):
    __tablename__ = "events"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(255), nullable=False)
    letterboxd_url = db.Column(db.String(512), nullable=False)
    poster_url = db.Column(db.String(512))
    synopsis = db.Column(db.Text)
    starts_at = db.Column(db.DateTime, nullable=False)
    location = db.Column(db.String(255), nullable=False)
    capacity = db.Column(db.Integer, nullable=False)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    invites = db.relationship(
        "Invite",
        back_populates="event",
        cascade="all, delete-orphan",
        order_by="Invite.created_at",
    )

    def confirmed_invites(self):
        return [invite for invite in self.invites if invite.status == "yes"]

    def waitlisted_invites(self):
        return [invite for invite in self.invites if invite.status == "waitlist"]

    def declined_invites(self):
        return [invite for invite in self.invites if invite.status == "no"]

    def available_seats(self) -> int:
        return max(self.capacity - len(self.confirmed_invites()), 0)


class Invite(db.Model):
    __tablename__ = "invites"

    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey("events.id"), nullable=False)
    email = db.Column(db.String(255), nullable=False)
    name = db.Column(db.String(255))
    token = db.Column(db.String(64), unique=True, nullable=False)
    status = db.Column(db.String(20), nullable=False, default="pending")
    seat_number = db.Column(db.Integer)
    responded_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    event = db.relationship("Event", back_populates="invites")

    def is_confirmed(self) -> bool:
        return self.status == "yes"

    def is_waitlisted(self) -> bool:
        return self.status == "waitlist"

    def display_name(self) -> str:
        return self.name or self.email

    def mark(self, status: str, seat_number: Optional[int] = None):
        self.status = status
        self.seat_number = seat_number
        self.responded_at = datetime.utcnow()


class MovieRequest(db.Model):
    __tablename__ = "movie_requests"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(255), nullable=False)
    letterboxd_url = db.Column(db.String(512))
    requester_name = db.Column(db.String(255), nullable=False)
    requester_email = db.Column(db.String(255), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="pending")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

