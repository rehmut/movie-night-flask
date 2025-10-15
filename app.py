from __future__ import annotations

import secrets
from datetime import datetime
from functools import wraps
from typing import List

from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from config import Config
from letterboxd import LetterboxdError, fetch_metadata, normalize_letterboxd_url
from models import Event, Invite, db


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.from_object(Config)

    db.init_app(app)

    with app.app_context():
        db.create_all()

    def is_admin() -> bool:
        return session.get("is_admin", False)

    def login_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not is_admin():
                flash("Please sign in to access the admin area.", "warning")
                return redirect(url_for("admin_login", next=request.path))
            return view(*args, **kwargs)

        return wrapped

    def generate_token() -> str:
        return secrets.token_urlsafe(16)

    def next_seat_number(event: Event, exclude_invite: int | None = None) -> int | None:
        taken = {
            invite.seat_number
            for invite in event.invites
            if invite.status == "yes" and invite.seat_number and invite.id != exclude_invite
        }
        for seat in range(1, event.capacity + 1):
            if seat not in taken:
                return seat
        return None

    def promote_waitlist(event: Event) -> List[Invite]:
        promoted: List[Invite] = []
        for invite in event.invites:
            if event.available_seats() <= 0:
                break
            if invite.status == "waitlist":
                seat = next_seat_number(event)
                if seat is None:
                    break
                invite.mark("yes", seat)
                promoted.append(invite)
        return promoted

    @app.context_processor
    def inject_utilities():
        return {
            "now": datetime.utcnow,
        }

    @app.route("/")
    def index():
        events = (
            Event.query.filter(Event.starts_at >= datetime.utcnow())
            .order_by(Event.starts_at.asc())
            .all()
        )
        past_events = (
            Event.query.filter(Event.starts_at < datetime.utcnow())
            .order_by(Event.starts_at.desc())
            .limit(3)
            .all()
        )
        return render_template(
            "index.html",
            events=events,
            past_events=past_events,
            is_admin=is_admin(),
        )

    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        if request.method == "POST":
            password = request.form.get("password", "")
            if password == app.config["ADMIN_PASSWORD"]:
                session["is_admin"] = True
                flash("Welcome back!", "success")
                next_url = request.args.get("next")
                return redirect(next_url or url_for("admin_dashboard"))
            flash("Incorrect password.", "danger")
        return render_template("admin_login.html")

    @app.route("/admin/logout")
    def admin_logout():
        session.pop("is_admin", None)
        flash("Signed out.", "info")
        return redirect(url_for("index"))

    @app.route("/admin")
    @login_required
    def admin_dashboard():
        events = Event.query.order_by(Event.starts_at.desc()).all()
        return render_template("admin_dashboard.html", events=events)

    @app.route("/admin/events/new", methods=["GET", "POST"])
    @login_required
    def admin_new_event():
        if request.method == "POST":
            letterboxd_url = request.form.get("letterboxd_url", "").strip()
            title = request.form.get("title", "").strip()
            synopsis = request.form.get("synopsis", "").strip()
            poster_url = request.form.get("poster_url", "").strip()
            starts_at_raw = request.form.get("starts_at", "").strip()
            location = request.form.get("location", "").strip()
            capacity = int(request.form.get("capacity", "0") or "0")
            notes = request.form.get("notes", "").strip() or None

            if not letterboxd_url or not starts_at_raw or not location or capacity <= 0:
                flash("Please fill in all required fields.", "warning")
                return render_template("admin_event_new.html")

            try:
                starts_at = datetime.strptime(starts_at_raw, "%Y-%m-%dT%H:%M")
            except ValueError:
                flash("Invalid date format.", "warning")
                return render_template("admin_event_new.html")

            normalized_url = None
            metadata = {}
            try:
                metadata = fetch_metadata(letterboxd_url)
                normalized_url = metadata.get("canonical_url")
            except LetterboxdError as exc:
                flash(f"Metadata fetch failed: {exc}", "warning")
                try:
                    normalized_url = normalize_letterboxd_url(letterboxd_url)
                except LetterboxdError:
                    normalized_url = letterboxd_url

            title = title or metadata.get("title") or "Untitled screening"
            synopsis = synopsis or metadata.get("synopsis")
            poster_url = poster_url or metadata.get("poster_url")

            event = Event(
                title=title,
                letterboxd_url=normalized_url or letterboxd_url,
                synopsis=synopsis,
                poster_url=poster_url,
                starts_at=starts_at,
                location=location,
                capacity=capacity,
                notes=notes,
            )
            db.session.add(event)
            db.session.commit()

            flash("Event created.", "success")
            return redirect(url_for("admin_event_detail", event_id=event.id))

        return render_template("admin_event_new.html")

    @app.route("/admin/events/<int:event_id>")
    @login_required
    def admin_event_detail(event_id: int):
        event = Event.query.get_or_404(event_id)
        invite_links = {
            invite.id: url_for("invite", token=invite.token, _external=True)
            for invite in event.invites
        }
        return render_template(
            "admin_event_detail.html",
            event=event,
            invite_links=invite_links,
        )

    @app.post("/admin/events/<int:event_id>/invites")
    @login_required
    def admin_add_invites(event_id: int):
        event = Event.query.get_or_404(event_id)
        emails_raw = request.form.get("emails", "")
        names_raw = request.form.get("names", "")
        emails = {
            entry.strip().lower()
            for entry in emails_raw.replace(";", "\n").replace(",", "\n").splitlines()
            if entry.strip()
        }
        names = [name.strip() for name in names_raw.splitlines() if name.strip()]

        if not emails:
            flash("Add at least one email.", "warning")
            return redirect(url_for("admin_event_detail", event_id=event.id))

        created = 0
        updated = 0
        for index, email in enumerate(sorted(emails)):
            invite = Invite.query.filter_by(event_id=event.id, email=email).first()
            name = names[index] if index < len(names) else None
            if invite:
                if name:
                    invite.name = name
                if invite.status == "pending":
                    invite.token = generate_token()
                updated += 1
            else:
                invite = Invite(
                    event=event,
                    email=email,
                    name=name,
                    token=generate_token(),
                )
                db.session.add(invite)
                created += 1

        db.session.commit()

        if created:
            flash(f"Created {created} invite(s). Share the RSVP links below.", "success")
        if updated:
            flash(f"Updated {updated} existing invite(s).", "info")
        return redirect(url_for("admin_event_detail", event_id=event.id))

    @app.route("/invite/<token>", methods=["GET", "POST"])
    def invite(token: str):
        invite = Invite.query.filter_by(token=token).first()
        if not invite:
            abort(404)
        event = invite.event

        if request.method == "POST":
            name = request.form.get("name", "").strip()
            status = request.form.get("status", "pending")
            if name:
                invite.name = name

            if status not in {"yes", "no", "waitlist"}:
                flash("Invalid selection.", "warning")
                return redirect(url_for("invite", token=token))

            if status == "yes":
                other_confirmed = [
                    i for i in event.invites if i.status == "yes" and i.id != invite.id
                ]
                if len(other_confirmed) < event.capacity:
                    seat = next_seat_number(event, exclude_invite=invite.id)
                    invite.mark("yes", seat)
                    message = (
                        f"Seat reserved! You are in seat {seat}."
                        if seat
                        else "Seat confirmed!"
                    )
                else:
                    invite.mark("waitlist", None)
                    message = "Event is full. You are on the waitlist."
            elif status == "waitlist":
                invite.mark("waitlist", None)
                message = "You are on the waitlist."
            else:  # status == "no"
                was_confirmed = invite.status == "yes"
                invite.mark("no", None)
                db.session.flush()
                promoted = []
                if was_confirmed:
                    promoted = promote_waitlist(event)
                message = "RSVP updated. Maybe next time!"
                if promoted:
                    promoted_names = ", ".join(p.display_name() for p in promoted)
                    message += f" Promoted from waitlist: {promoted_names}."

            db.session.commit()
            flash(message, "success")
            return redirect(url_for("invite", token=token))

        confirmed_count = len(event.confirmed_invites())
        return render_template(
            "invite.html",
            event=event,
            invite=invite,
            confirmed_count=confirmed_count,
            seats_remaining=event.available_seats(),
        )

    @app.errorhandler(404)
    def not_found(_: Exception):
        return render_template("404.html"), 404

    return app


app = create_app()


if __name__ == "__main__":
    app.run(debug=True)
