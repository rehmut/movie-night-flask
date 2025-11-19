from __future__ import annotations

import base64
import csv
import io
import secrets
from datetime import datetime
from functools import wraps
from typing import List

import qrcode
from flask import (
    Flask,
    Response,
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
from models import Event, Invite, db, MovieRequest


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
                flash("Bitte melde dich an, um den Adminbereich zu öffnen.", "warning")
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

    def resolve_letterboxd_metadata(letterboxd_url: str) -> tuple[str, dict[str, str], str | None]:
        metadata: dict[str, str] = {}
        normalized_url: str | None = None
        warning: str | None = None
        try:
            metadata = fetch_metadata(letterboxd_url)
            normalized_url = metadata.get("canonical_url")
        except LetterboxdError as exc:
            warning = f"Letterboxd-Daten konnten nicht geladen werden: {exc}"
            try:
                normalized_url = normalize_letterboxd_url(letterboxd_url)
            except LetterboxdError:
                normalized_url = letterboxd_url
        return normalized_url or letterboxd_url, metadata, warning

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
                flash("Willkommen zurück!", "success")
                next_url = request.args.get("next")
                return redirect(next_url or url_for("admin_dashboard"))
            flash("Passwort stimmt nicht.", "danger")
        return render_template("admin_login.html")

    @app.route("/admin/logout")
    def admin_logout():
        session.pop("is_admin", None)
        flash("Abgemeldet.", "info")
        return redirect(url_for("index"))

    @app.route("/admin")
    @login_required
    def admin_dashboard():
        events = Event.query.order_by(Event.starts_at.desc()).all()
        requests = MovieRequest.query.order_by(MovieRequest.created_at.desc()).all()
        return render_template("admin_dashboard.html", events=events, requests=requests)

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
            capacity_raw = request.form.get("capacity", "0").strip()
            notes = request.form.get("notes", "").strip() or None

            if not letterboxd_url or not starts_at_raw or not location:
                flash("Bitte fülle alle Pflichtfelder aus.", "warning")
                return render_template("admin_event_new.html")

            try:
                starts_at = datetime.strptime(starts_at_raw, "%Y-%m-%dT%H:%M")
            except ValueError:
                flash("Ungültiges Datumsformat.", "warning")
                return render_template("admin_event_new.html")

            try:
                capacity = int(capacity_raw or "0")
            except ValueError:
                capacity = 0

            if capacity <= 0:
                flash("Lege mindestens einen Platz fest.", "warning")
                return render_template("admin_event_new.html")

            normalized_url, metadata, warning = resolve_letterboxd_metadata(letterboxd_url)
            if warning:
                flash(warning, "warning")

            title = title or metadata.get("title") or "Noch ohne Titel"
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

            flash("Event erstellt.", "success")
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

    @app.route("/admin/events/<int:event_id>/edit", methods=["GET", "POST"])
    @login_required
    def admin_edit_event(event_id: int):
        event = Event.query.get_or_404(event_id)

        if request.method == "POST":
            letterboxd_url = request.form.get("letterboxd_url", "").strip()
            title = request.form.get("title", "").strip()
            synopsis = request.form.get("synopsis", "").strip()
            poster_url = request.form.get("poster_url", "").strip()
            starts_at_raw = request.form.get("starts_at", "").strip()
            location = request.form.get("location", "").strip()
            capacity_raw = request.form.get("capacity", "0").strip()
            notes = request.form.get("notes", "").strip() or None

            if not letterboxd_url or not starts_at_raw or not location:
                flash("Bitte fülle alle Pflichtfelder aus.", "warning")
                return render_template("admin_event_edit.html", event=event)

            try:
                starts_at = datetime.strptime(starts_at_raw, "%Y-%m-%dT%H:%M")
            except ValueError:
                flash("Ungültiges Datumsformat.", "warning")
                return render_template("admin_event_edit.html", event=event)

            try:
                capacity = int(capacity_raw or "0")
            except ValueError:
                capacity = 0

            if capacity <= 0:
                flash("Lege mindestens einen Platz fest.", "warning")
                return render_template("admin_event_edit.html", event=event)

            normalized_url, metadata, warning = resolve_letterboxd_metadata(letterboxd_url)
            if warning:
                flash(warning, "warning")

            title = title or metadata.get("title") or "Noch ohne Titel"
            synopsis = synopsis or metadata.get("synopsis")
            poster_url = poster_url or metadata.get("poster_url")

            event.title = title
            event.letterboxd_url = normalized_url
            event.synopsis = synopsis
            event.poster_url = poster_url
            event.starts_at = starts_at
            event.location = location
            event.capacity = capacity
            event.notes = notes

            db.session.commit()
            flash("Event aktualisiert.", "success")
            return redirect(url_for("admin_event_detail", event_id=event.id))

        return render_template("admin_event_edit.html", event=event)

    @app.post("/admin/events/<int:event_id>/delete")
    @login_required
    def admin_delete_event(event_id: int):
        event = Event.query.get_or_404(event_id)
        db.session.delete(event)
        db.session.commit()
        flash("Event gelöscht.", "info")
        return redirect(url_for("admin_dashboard"))

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
            flash("Bitte mindestens eine E-Mail eintragen.", "warning")
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
            word_created = "Einladung" if created == 1 else "Einladungen"
            flash(f"{created} {word_created} erzeugt. Die Links findest du unten.", "success")
        if updated:
            word_updated = "Einladung" if updated == 1 else "Einladungen"
            flash(f"{updated} bestehende {word_updated} aktualisiert.", "info")
        return redirect(url_for("admin_event_detail", event_id=event.id))

    @app.get("/admin/events/<int:event_id>/invites/export")
    @login_required
    def admin_export_invites(event_id: int):
        event = Event.query.get_or_404(event_id)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "Name",
            "Email",
            "Status",
            "Sitz",
            "Einladungslink",
            "QR-Code",
        ])
        status_map = {"yes": "Zusage", "waitlist": "Warteliste", "no": "Absage", "pending": "Offen"}
        for invite in event.invites:
            invite_link = url_for("invite", token=invite.token, _external=True)
            qr_image = qrcode.make(invite_link)
            buffer = io.BytesIO()
            qr_image.save(buffer, format="PNG")
            qr_data = base64.b64encode(buffer.getvalue()).decode("ascii")
            qr_data_uri = f"data:image/png;base64,{qr_data}"
            writer.writerow(
                [
                    invite.display_name(),
                    invite.email,
                    status_map.get(invite.status, invite.status),
                    invite.seat_number or "",
                    invite_link,
                    qr_data_uri,
                ]
            )
        response = Response(output.getvalue(), mimetype="text/csv")
        filename = f"event-{event.id}-invites.csv"
        response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response

    @app.post("/events/<int:event_id>/request-invite")
    def request_invite(event_id: int):
        event = Event.query.get_or_404(event_id)
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()

        if not name or not email:
            flash("Bitte fülle alle Pflichtfelder aus.", "warning")
            return redirect(url_for("invite", token=event.invites[0].token if event.invites else ""))

        existing_invite = Invite.query.filter_by(event_id=event.id, email=email).first()
        if existing_invite:
            flash("Du hast bereits eine Einladung für dieses Event angefordert oder erhalten.", "info")
            return redirect(url_for("index"))

        invite = Invite(
            event=event,
            email=email,
            name=name,
            token=generate_token(),
            status="requested",
        )
        db.session.add(invite)
        db.session.commit()

        flash("Deine Einladungsanfrage wurde übermittelt.", "success")
        return redirect(url_for("index"))

    @app.post("/admin/invites/<int:invite_id>/approve")
    @login_required
    def admin_approve_invite(invite_id: int):
        invite = Invite.query.get_or_404(invite_id)
        invite.status = "pending"
        db.session.commit()
        flash("Einladungsanfrage genehmigt.", "success")
        return redirect(url_for("admin_event_detail", event_id=invite.event_id))

    @app.post("/admin/invites/<int:invite_id>/reject")
    @login_required
    def admin_reject_invite(invite_id: int):
        invite = Invite.query.get_or_404(invite_id)
        db.session.delete(invite)
        db.session.commit()
        flash("Einladungsanfrage abgelehnt.", "info")
        return redirect(url_for("admin_event_detail", event_id=invite.event_id))

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
                flash("Ungültige Auswahl.", "warning")
                return redirect(url_for("invite", token=token))

            if status == "yes":
                other_confirmed = [
                    i for i in event.invites if i.status == "yes" and i.id != invite.id
                ]
                if len(other_confirmed) < event.capacity:
                    seat = next_seat_number(event, exclude_invite=invite.id)
                    invite.mark("yes", seat)
                    message = (
                        f"Platz reserviert! Du sitzt auf Platz {seat}."
                        if seat
                        else "Platz bestätigt!"
                    )
                else:
                    invite.mark("waitlist", None)
                    message = "Event ist voll. Du stehst auf der Warteliste."
            elif status == "waitlist":
                invite.mark("waitlist", None)
                message = "Du stehst auf der Warteliste."
            else:  # status == "no"
                was_confirmed = invite.status == "yes"
                invite.mark("no", None)
                db.session.flush()
                promoted = []
                if was_confirmed:
                    promoted = promote_waitlist(event)
                message = "Antwort gespeichert. Vielleicht klappt es beim nächsten Mal!"
                if promoted:
                    promoted_names = ", ".join(p.display_name() for p in promoted)
                    message += f" Warteliste nachgerückt: {promoted_names}."

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

    @app.route("/requests", methods=["GET", "POST"])
    def movie_requests():
        if request.method == "POST":
            title = request.form.get("title", "").strip()
            letterboxd_url = request.form.get("letterboxd_url", "").strip()

            if not title:
                flash("Bitte fülle alle Pflichtfelder aus.", "warning")
                return redirect(url_for("movie_requests"))

            poster_url = None
            if letterboxd_url:
                try:
                    normalized_url, metadata, warning = resolve_letterboxd_metadata(letterboxd_url)
                    if warning:
                        flash(warning, "warning")
                    title = metadata.get("title") or title
                    poster_url = metadata.get("poster_url")
                    letterboxd_url = normalized_url
                except LetterboxdError as e:
                    flash(str(e), "danger")

            movie_request = MovieRequest(
                title=title,
                letterboxd_url=letterboxd_url,
                poster_url=poster_url,
            )
            db.session.add(movie_request)
            db.session.commit()

            flash("Dein Filmwunsch wurde übermittelt.", "success")
            return redirect(url_for("movie_requests"))

        requests = MovieRequest.query.order_by(MovieRequest.created_at.desc()).all()
        return render_template("requests.html", requests=requests, is_admin=is_admin())

    @app.route("/admin/requests")
    @login_required
    def admin_requests():
        requests = MovieRequest.query.order_by(MovieRequest.created_at.desc()).all()
        return render_template("admin_requests.html", requests=requests)

    @app.post("/admin/requests/<int:request_id>/approve")
    @login_required
    def admin_approve_request(request_id: int):
        movie_request = MovieRequest.query.get_or_404(request_id)
        movie_request.status = "approved"
        db.session.commit()
        flash("Filmwunsch genehmigt.", "success")
        return redirect(url_for("admin_requests"))

    @app.post("/admin/requests/<int:request_id>/reject")
    @login_required
    def admin_reject_request(request_id: int):
        movie_request = MovieRequest.query.get_or_404(request_id)
        movie_request.status = "rejected"
        db.session.commit()
        flash("Filmwunsch abgelehnt.", "info")
        return redirect(url_for("admin_requests"))

    @app.errorhandler(404)
    def not_found(_: Exception):
        return render_template("404.html"), 404

    return app


app = create_app()


if __name__ == "__main__":
    app.run(debug=True)



