from __future__ import annotations

import base64
import csv
import io
import secrets
from datetime import datetime
from functools import wraps
from typing import List

import qrcode
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
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
from letterboxd import (
    LetterboxdError,
    fetch_metadata,
    normalize_letterboxd_url,
    title_from_letterboxd_url,
    search_metadata,
    FilmChoicesRequired,
)
from models import Event, Invite, db, MovieRequest, MovieVote, MovieIdentity
from wish_helpers import film_keys, name_key, same_film


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.from_object(Config)

    db.init_app(app)

    with app.app_context():
        db.create_all()
        columns = {column["name"] for column in inspect(db.engine).get_columns("movie_requests")}
        if "suggester_name" not in columns:
            db.session.execute(
                text("ALTER TABLE movie_requests ADD COLUMN suggester_name VARCHAR(255)")
            )
            db.session.commit()

    def is_admin() -> bool:
        return session.get("is_admin", False)

    def screened_film(title: str, letterboxd_url: str = "") -> bool:
        return any(
            same_film(title, letterboxd_url, event.title, event.letterboxd_url)
            for event in Event.query.filter(Event.starts_at < datetime.utcnow()).all()
        )

    def active_movie_requests() -> list[MovieRequest]:
        past_events = Event.query.filter(Event.starts_at < datetime.utcnow()).all()
        return [
            movie_request
            for movie_request in MovieRequest.query.order_by(
                MovieRequest.created_at.desc()
            ).all()
            if not any(
                same_film(
                    movie_request.title,
                    movie_request.letterboxd_url,
                    event.title,
                    event.letterboxd_url,
                )
                for event in past_events
            )
        ]

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
                fallback_title = title_from_letterboxd_url(normalized_url)
                if fallback_title:
                    metadata["title"] = fallback_title
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
        screened_request_ids = {
            item.id
            for item in requests
            if screened_film(item.title, item.letterboxd_url)
        }
        return render_template(
            "admin_dashboard.html",
            events=events,
            requests=requests,
            screened_request_ids=screened_request_ids,
        )

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

        if not name:
            flash("Bitte gib deinen Namen an.", "warning")
            return redirect(url_for("index"))

        # Check for existing invite by name (since email is gone)
        existing_invite = Invite.query.filter_by(event_id=event.id, name=name).first()
        if existing_invite:
            flash("Du hast bereits einen Platz reserviert.", "info")
            return redirect(url_for("index"))

        # Logic: Max 6 people. If < 6 confirmed, auto-accept. Else waitlist.
        confirmed_count = len(event.confirmed_invites())
        limit = 6

        if confirmed_count < limit:
            status = "yes"
            seat = next_seat_number(event)
            message = f"Platz reserviert! Du sitzt auf Platz {seat}."
        else:
            status = "waitlist"
            seat = None
            message = "Event ist voll. Du stehst auf der Warteliste."

        # Generate a fake email to satisfy DB constraint if column is not nullable
        fake_email = f"{secrets.token_hex(8)}@placeholder.local"

        invite = Invite(
            event=event,
            email=fake_email,
            name=name,
            token=generate_token(),
            status=status,
            seat_number=seat,
            responded_at=datetime.utcnow() if status == "yes" else None,
        )
        db.session.add(invite)
        db.session.commit()

        flash(message, "success")
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
            suggester_name = request.form.get("suggester_name", "").strip()
            letterboxd_url = request.form.get("letterboxd_url", "").strip()

            if not title or not suggester_name or len(title) > 255 or len(suggester_name) > 255:
                flash("Bitte fülle alle Pflichtfelder aus.", "warning")
                return redirect(url_for("movie_requests"))

            poster_url = None
            original_title = title
            if letterboxd_url and screened_film(title, letterboxd_url):
                flash("Dieser Film ist bei uns bereits gelaufen.", "info")
                return redirect(url_for("movie_requests"))
            if any(
                film_keys(title) & film_keys(item.title, item.letterboxd_url)
                for item in active_movie_requests()
            ):
                flash("Dieser Film wurde bereits vorgeschlagen. Stimme beim vorhandenen Wunsch ab.", "info")
                return redirect(url_for("movie_requests"))
            if letterboxd_url:
                try:
                    letterboxd_url = normalize_letterboxd_url(letterboxd_url)
                    if not title_from_letterboxd_url(letterboxd_url) or len(letterboxd_url) > 512:
                        raise LetterboxdError("Bitte einen Letterboxd-Filmlink angeben.")
                    normalized_url, metadata, warning = resolve_letterboxd_metadata(letterboxd_url)
                    if warning:
                        flash(warning, "warning")
                    title = metadata.get("title") or title
                    poster_url = metadata.get("poster_url")
                    letterboxd_url = normalized_url
                except LetterboxdError as e:
                    flash(str(e), "danger")
                    return redirect(url_for("movie_requests"))
            else:
                try:
                    metadata = search_metadata(title)
                    title = metadata.get("title") or title
                    poster_url = metadata.get("poster_url")
                    letterboxd_url = metadata.get("canonical_url") or ""
                except FilmChoicesRequired as exc:
                    return render_template("request_choices.html", choices=exc.choices, suggester_name=suggester_name)
                except LetterboxdError as exc:
                    flash(str(exc), "warning")

            keys = film_keys(title, letterboxd_url) | film_keys(original_title)
            if screened_film(title, letterboxd_url):
                flash("Dieser Film ist bei uns bereits gelaufen.", "info")
                return redirect(url_for("movie_requests"))
            existing = MovieIdentity.query.filter(MovieIdentity.key.in_(keys)).first()
            duplicate = existing is not None or any(
                keys & film_keys(item.title, item.letterboxd_url)
                for item in MovieRequest.query.all()
            )
            if duplicate:
                flash("Dieser Film wurde bereits vorgeschlagen. Stimme beim vorhandenen Wunsch ab.", "info")
                return redirect(url_for("movie_requests"))

            movie_request = MovieRequest(
                title=title,
                suggester_name=suggester_name,
                letterboxd_url=letterboxd_url,
                poster_url=poster_url,
            )
            db.session.add(movie_request)
            try:
                db.session.flush()
                db.session.add_all(MovieIdentity(key=key, request_id=movie_request.id) for key in keys)
                db.session.add(
                    MovieVote(
                        request_id=movie_request.id,
                        name=suggester_name,
                        name_key=name_key(suggester_name),
                    )
                )
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                flash("Dieser Film wurde bereits vorgeschlagen.", "info")
                return redirect(url_for("movie_requests"))

            flash("Dein Filmwunsch wurde übermittelt.", "success")
            return redirect(url_for("movie_requests"))

        requests = active_movie_requests()
        query = request.args.get("q", "").strip()
        status = request.args.get("status", "")
        if query:
            requests = [item for item in requests if name_key(query) in name_key(item.title)]
        if status in {"pending", "approved", "rejected"}:
            requests = [item for item in requests if item.status == status]
        if request.args.get("sort", "votes") == "votes":
            requests.sort(key=lambda item: len(item.votes), reverse=True)
        return render_template("requests.html", requests=requests, is_admin=is_admin())

    @app.post("/requests/vote")
    def vote_movies():
        name = request.form.get("name", "").strip()
        request_ids = {
            int(value)
            for value in request.form.getlist("request_ids")
            if value.isdigit()
        }

        if not name or len(name) > 255:
            flash("Bitte gib deinen Namen an (maximal 255 Zeichen).", "warning")
            return redirect(url_for("movie_requests"))
        if not request_ids:
            flash("Bitte wähle mindestens einen Film aus.", "warning")
            return redirect(url_for("movie_requests"))

        normalized_name = name_key(name)
        movies = MovieRequest.query.filter(MovieRequest.id.in_(request_ids)).all()
        eligible_movies = [
            movie
            for movie in movies
            if movie.status != "rejected"
            and not screened_film(movie.title, movie.letterboxd_url)
        ]
        existing_ids = {
            vote.request_id
            for vote in MovieVote.query.filter(
                MovieVote.request_id.in_([movie.id for movie in eligible_movies]),
                MovieVote.name_key == normalized_name,
            ).all()
        }
        new_votes = [
            MovieVote(request_id=movie.id, name=name, name_key=normalized_name)
            for movie in eligible_movies
            if movie.id not in existing_ids
        ]

        if not new_votes:
            flash("Deine Auswahl war bereits gespeichert.", "info")
            return redirect(url_for("movie_requests"))

        db.session.add_all(new_votes)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash("Deine Auswahl war bereits gespeichert.", "info")
            return redirect(url_for("movie_requests"))

        count = len(new_votes)
        flash(
            f"Deine {'Stimme wurde' if count == 1 else 'Stimmen wurden'} gespeichert.",
            "success",
        )
        return redirect(url_for("movie_requests"))

    @app.post("/requests/<int:request_id>/vote")
    def vote_movie(request_id):
        movie = MovieRequest.query.get_or_404(request_id)
        name = request.form.get("name", "").strip()
        if not name or len(name) > 255:
            flash("Bitte gib deinen Namen an (maximal 255 Zeichen).", "warning")
        elif screened_film(movie.title, movie.letterboxd_url):
            flash("Dieser Film ist bei uns bereits gelaufen.", "info")
        elif movie.status == "rejected":
            flash("Dieser Wunsch ist bereits abgelehnt.", "warning")
        else:
            db.session.add(MovieVote(request_id=movie.id, name=name, name_key=name_key(name)))
            try:
                db.session.commit()
                flash("Deine Stimme wurde gespeichert.", "success")
            except IntegrityError:
                db.session.rollback()
                flash("Unter diesem Namen wurde bereits abgestimmt.", "info")
        return redirect(url_for("movie_requests", _anchor="wish-" + str(request_id)))

    @app.cli.command("refresh-request-posters")
    def refresh_request_posters():
        """Fill missing posters on existing requests without changing their titles."""
        import click
        for item in active_movie_requests():
            if item.poster_url and "backdrop" not in item.poster_url:
                continue
            try:
                metadata = fetch_metadata(item.letterboxd_url) if item.letterboxd_url else search_metadata(item.title)
                candidate_url = metadata.get("canonical_url") or item.letterboxd_url
                if candidate_url and any(
                    other.id != item.id and film_keys("", candidate_url) & (film_keys("", other.letterboxd_url) - {"title:"})
                    for other in MovieRequest.query.all()
                ):
                    click.echo(f"{item.id}: Film bereits vorhanden; bitte manuell pruefen")
                    continue
                item.poster_url = metadata.get("poster_url")
                item.letterboxd_url = metadata.get("canonical_url") or item.letterboxd_url
                db.session.commit()
                click.echo(f"{item.id}: {'Plakat geladen' if item.poster_url else 'Kein Plakat gefunden'}")
            except LetterboxdError as exc:
                click.echo(f"{item.id}: {exc}")

    @app.route("/admin/requests")
    @login_required
    def admin_requests():
        requests = MovieRequest.query.order_by(MovieRequest.created_at.desc()).all()
        screened_request_ids = {
            item.id
            for item in requests
            if screened_film(item.title, item.letterboxd_url)
        }
        return render_template(
            "admin_requests.html",
            requests=requests,
            screened_request_ids=screened_request_ids,
        )

    @app.route("/admin/requests/<int:request_id>/edit", methods=["GET", "POST"])
    @login_required
    def admin_edit_request(request_id: int):
        movie_request = MovieRequest.query.get_or_404(request_id)

        if request.method == "POST":
            title = request.form.get("title", "").strip()
            suggester_name = request.form.get("suggester_name", "").strip()
            letterboxd_url = request.form.get("letterboxd_url", "").strip()
            poster_url = request.form.get("poster_url", "").strip()
            status = request.form.get("status", "pending")

            if (
                not title
                or not suggester_name
                or len(title) > 255
                or len(suggester_name) > 255
                or status not in {"pending", "approved", "rejected"}
            ):
                flash("Bitte prüfe die Pflichtfelder.", "warning")
                return render_template(
                    "admin_request_edit.html", movie_request=movie_request
                )

            if letterboxd_url:
                try:
                    letterboxd_url = normalize_letterboxd_url(letterboxd_url)
                    if not title_from_letterboxd_url(letterboxd_url):
                        raise LetterboxdError(
                            "Bitte einen Letterboxd-Filmlink angeben."
                        )
                except LetterboxdError as exc:
                    flash(str(exc), "danger")
                    return render_template(
                        "admin_request_edit.html", movie_request=movie_request
                    )

                link_changed = letterboxd_url != movie_request.letterboxd_url
                if link_changed or not poster_url:
                    try:
                        metadata = fetch_metadata(letterboxd_url)
                        poster_url = metadata.get("poster_url") or poster_url
                    except LetterboxdError as exc:
                        flash(
                            f"Letterboxd-Daten konnten nicht aktualisiert werden: {exc}",
                            "warning",
                        )

            duplicate = any(
                other.id != movie_request.id
                and same_film(title, letterboxd_url, other.title, other.letterboxd_url)
                for other in MovieRequest.query.all()
            )
            if duplicate:
                flash("Dieser Filmwunsch existiert bereits.", "warning")
                return render_template(
                    "admin_request_edit.html", movie_request=movie_request
                )

            keys = film_keys(title, letterboxd_url)
            try:
                MovieIdentity.query.filter_by(request_id=movie_request.id).delete()
                db.session.flush()
                db.session.add_all(
                    MovieIdentity(key=key, request_id=movie_request.id) for key in keys
                )
                movie_request.title = title
                movie_request.suggester_name = suggester_name
                movie_request.letterboxd_url = letterboxd_url or None
                movie_request.poster_url = poster_url or None
                movie_request.status = status
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                flash("Dieser Filmwunsch existiert bereits.", "warning")
                return render_template(
                    "admin_request_edit.html", movie_request=movie_request
                )

            flash("Filmwunsch aktualisiert.", "success")
            return redirect(url_for("admin_dashboard", tab="requests") + "#requests")

        return render_template("admin_request_edit.html", movie_request=movie_request)

    @app.post("/admin/requests/<int:request_id>/delete")
    @login_required
    def admin_delete_request(request_id: int):
        movie_request = MovieRequest.query.get_or_404(request_id)
        MovieIdentity.query.filter_by(request_id=movie_request.id).delete()
        db.session.delete(movie_request)
        db.session.commit()
        flash("Filmwunsch gelöscht.", "info")
        return redirect(url_for("admin_dashboard", tab="requests") + "#requests")

    @app.post("/admin/requests/<int:request_id>/approve")
    @login_required
    def admin_approve_request(request_id: int):
        movie_request = MovieRequest.query.get_or_404(request_id)
        movie_request.status = "approved"
        db.session.commit()
        flash("Filmwunsch genehmigt.", "success")
        return redirect(url_for("admin_dashboard", tab="requests") + "#requests")

    @app.post("/admin/requests/<int:request_id>/reject")
    @login_required
    def admin_reject_request(request_id: int):
        movie_request = MovieRequest.query.get_or_404(request_id)
        movie_request.status = "rejected"
        db.session.commit()
        flash("Filmwunsch abgelehnt.", "info")
        return redirect(url_for("admin_dashboard", tab="requests") + "#requests")

    @app.errorhandler(404)
    def not_found(_: Exception):
        return render_template("404.html"), 404

    return app


app = create_app()


if __name__ == "__main__":
    app.run(debug=True)



