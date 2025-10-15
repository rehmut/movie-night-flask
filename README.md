# Movie Night (Flask Edition)

Host private movie nights with RSVP tracking, waitlists, and Letterboxd-powered metadata. Built for easy deployment on [PythonAnywhere](https://www.pythonanywhere.com/), using Flask and SQLite (no external database needed).

## Features
- Admin console secured by password (`ADMIN_PASSWORD`).
- Create events from a Letterboxd link; metadata auto-fills title, poster, and synopsis (with manual overrides).
- Generate invite links for guests; track confirmed, waitlist, and declined responses.
- Seat reservations respect capacity and auto-promote waitlisted guests when seats free up.
- Sleek dark UI for both guests and hosts.

## Requirements
- Python 3.11+ (PythonAnywhere free tier ships with compatible versions).
- SQLite (bundled with Python).

## Local Setup
```bash
cd movie-night-flask
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
set FLASK_APP=app.py             # Windows cmd
$env:FLASK_APP="app.py"          # PowerShell
set FLASK_ENV=development        # optional for auto reload
python -m flask run
```

Environment variables (optional locally, required in production):
```
SECRET_KEY=change-me
ADMIN_PASSWORD=choose-a-strong-password
```

The default database `movie_night.db` is created automatically in the project folder.

## PythonAnywhere Deployment
1. **Create a new PythonAnywhere app**
   - Sign in ? Dashboard ? "Add a new web app".
   - Choose "Manual configuration" ? "Flask" ? Python 3.x (match your project’s Python version).

2. **Upload project files**
   - In the Files tab, create a directory (e.g., `movie-night-flask`) and upload the repository contents.
   - Alternatively, push to GitHub and clone it on PythonAnywhere using `git clone` in the Bash console.

3. **Create a virtualenv**
   ```bash
   cd ~/movie-night-flask
   python -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

4. **Configure environment variables**
   - In the PythonAnywhere dashboard ? Web tab ? click your web app ? section "Environment variables".
   - Add:
     - `SECRET_KEY` set to a random string.
     - `ADMIN_PASSWORD` set to your chosen admin password.

5. **Point the WSGI config to the app**
   - In the Web tab, open the WSGI configuration file link.
   - Update it to add the project to the path and expose `app`:
     ```python
     import sys
     sys.path.insert(0, '/home/<username>/movie-night-flask')

     from app import app as application
     ```
   - Save the file.

6. **Reload the web app**
   - In the Web tab, click "Reload". Your site is live.

7. **Access the admin dashboard**
   - Browse to your PythonAnywhere URL, click "Admin", and sign in with `ADMIN_PASSWORD`.
   - Create events, generate invites, and share links (they’re shown in the roster table).

## Project Structure
```
movie-night-flask/
+-- app.py              # Flask routes and application factory
+-- config.py           # Configuration and environment defaults
+-- letterboxd.py       # Metadata fetch helper
+-- models.py           # SQLAlchemy models
+-- requirements.txt    # Dependencies
+-- static/styles.css   # Styling
+-- templates/          # Jinja templates
```

## Customization Ideas
- Hook up transactional email (e.g., via SendGrid or Mailgun) to send invite links automatically.
- Add calendar ICS downloads per event.
- Extend the roster with custom fields (meal preferences, guest notes, etc.).

Enjoy your screenings! ??
