import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
from app import create_app
from models import db, Event, MovieRequest, MovieVote
from letterboxd import search_metadata, fetch_metadata, LetterboxdError, FilmChoicesRequired


class WishTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config.update(TESTING=True)
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        db.engine.dispose()
        self.ctx.pop()

    def submit(self, title="The Matrix", name="Mira", url=""):
        return self.client.post("/requests", data={"title": title, "suggester_name": name, "letterboxd_url": url})

    @patch("app.search_metadata")
    def test_search_duplicate_and_votes(self, search):
        search.return_value = {"title": "The Matrix (1999)", "poster_url": "https://example.com/poster.jpg", "canonical_url": "https://letterboxd.com/film/the-matrix/"}
        self.assertEqual(self.submit().status_code, 302)
        item = MovieRequest.query.one()
        self.assertTrue(item.poster_url)
        self.assertEqual(MovieVote.query.one().name, "Mira")
        self.submit(title=" THE   MATRIX ", name="Leo")
        self.assertEqual(MovieRequest.query.count(), 1)
        for name in ["Mira", " mira ", "MIRA", "Leo", "   "]:
            self.client.post(f"/requests/{item.id}/vote", data={"name": name})
        self.assertEqual(MovieVote.query.count(), 2)
        page = self.client.get("/requests").get_data(as_text=True)
        self.assertIn("2 Stimmen", page)
        self.assertIn("Mira", page)
        self.assertNotIn("The Matrix", self.client.get("/requests?q=unknown").get_data(as_text=True))

    @patch("app.search_metadata", side_effect=LetterboxdError("blocked"))
    def test_unavailable_search_and_validation(self, search):
        self.submit(name=" ")
        self.assertEqual(MovieRequest.query.count(), 0)
        self.submit(url="https://evilletterboxd.com/film/matrix/")
        self.assertEqual(MovieRequest.query.count(), 0)
        self.submit()
        item = MovieRequest.query.one()
        self.assertIsNone(item.poster_url)
        self.assertEqual(MovieVote.query.count(), 1)
        self.assertNotIn("wish-poster\"", self.client.get("/requests").get_data(as_text=True))
        item.status = "rejected"
        db.session.commit()
        self.client.post(f"/requests/{item.id}/vote", data={"name": "Mira"})
        self.assertEqual(MovieVote.query.count(), 1)

    def test_create_form_is_visible_and_filters_are_collapsed(self):
        page = self.client.get("/requests").get_data(as_text=True)
        self.assertIn('<section class="wish-create">', page)
        self.assertIn('<details class="wish-filter-panel"', page)
        self.assertNotIn('<details class="wish-create">', page)

        filtered_page = self.client.get("/requests?q=Alien").get_data(as_text=True)
        self.assertIn('<details class="wish-filter-panel" open>', filtered_page)

    @patch("letterboxd.fetch_metadata")
    @patch("letterboxd.requests.get")
    def test_search_exact_and_ambiguous(self, get, fetch):
        get.return_value = Mock(text='<a href="/film/the-matrix/">The Matrix</a>')
        fetch.return_value = {"title": "The Matrix"}
        self.assertEqual(search_metadata("the matrix")["title"], "The Matrix")
        get.return_value.text += '<a href="/film/the-matrix-2/">The Matrix</a>'
        with self.assertRaises(LetterboxdError):
            search_metadata("The Matrix")

    @patch("app.search_metadata")
    def test_refresh_existing(self, search):
        db.session.add(MovieRequest(title="Alien", suggester_name="Mira"))
        db.session.commit()
        search.return_value = {"poster_url": "https://example.com/alien.jpg", "canonical_url": "https://letterboxd.com/film/alien/"}
        result = self.app.test_cli_runner().invoke(args=["refresh-request-posters"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(MovieRequest.query.one().poster_url)

    @patch("letterboxd.requests.get")
    def test_ajax_search_and_real_poster(self, get):
        get.side_effect = [
            Mock(text='<a class="load-more-search" data-url="/s/search/films/matrix/">More</a>'),
            Mock(text='<article><h2><a href="/film/the-matrix/">The Matrix</a> 1999</h2></article>'),
            Mock(status_code=200, text='<meta property="og:title" content="The Matrix (1999)"><meta property="og:image" content="https://example.com/backdrop.jpg"><script type="application/ld+json">{"@type":"Movie","image":"https://a.ltrbxd.com/film-poster/matrix.jpg"}</script>'),
        ]
        metadata = search_metadata("The Matrix 1999")
        self.assertIn("film-poster", metadata["poster_url"])
        self.assertEqual(get.call_count, 3)

    @patch("app.search_metadata")
    def test_choices_do_not_create_wish(self, search):
        search.side_effect = FilmChoicesRequired([{"title": "The Matrix 1999", "canonical_url": "https://letterboxd.com/film/the-matrix/"}])
        response = self.submit()
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'letterboxd_url', response.data)
        self.assertEqual(MovieRequest.query.count(), 0)

    @patch("app.search_metadata")
    def test_screened_films_are_hidden_and_blocked(self, search):
        event = Event(
            title="The Matrix (1999)",
            letterboxd_url="https://letterboxd.com/film/the-matrix/",
            starts_at=datetime.utcnow() - timedelta(days=1),
            location="Kino",
            capacity=6,
        )
        movie_request = MovieRequest(
            title="Matrix",
            letterboxd_url="https://www.letterboxd.com/film/the-matrix?ref=test",
        )
        db.session.add_all([event, movie_request])
        db.session.commit()

        self.assertNotIn(b"Matrix", self.client.get("/requests").data)
        with self.client.session_transaction() as session:
            session["is_admin"] = True
        admin_page = self.client.get("/admin/requests").data
        self.assertIn(b"Matrix", admin_page)
        self.assertIn(b"Gelaufen", admin_page)

        self.client.post(
            f"/requests/{movie_request.id}/vote", data={"name": "Mira"}
        )
        self.assertEqual(MovieVote.query.count(), 0)

        search.return_value = {
            "title": "The Matrix (1999)",
            "canonical_url": "https://letterboxd.com/film/the-matrix/",
        }
        self.submit(title="THE MATRIX")
        search.assert_called_once()
        self.assertEqual(MovieRequest.query.count(), 1)

        search.reset_mock()
        search.return_value = {
            "title": "The Matrix (1999)",
            "canonical_url": "https://letterboxd.com/film/the-matrix/",
        }
        self.submit(title="Ein alternativer Titel")
        self.assertEqual(MovieRequest.query.count(), 1)

    def test_future_events_and_remakes_remain_visible(self):
        db.session.add(
            Event(
                title="Alien (1979)",
                letterboxd_url="https://letterboxd.com/film/alien/",
                starts_at=datetime.utcnow() + timedelta(days=1),
                location="Kino",
                capacity=6,
            )
        )
        db.session.add(MovieRequest(title="Alien"))
        db.session.add(
            Event(
                title="Suspiria (1977)",
                letterboxd_url="https://letterboxd.com/film/suspiria/",
                starts_at=datetime.utcnow() - timedelta(days=1),
                location="Kino",
                capacity=6,
            )
        )
        db.session.add(
            MovieRequest(
                title="Suspiria (2018)",
                letterboxd_url="https://letterboxd.com/film/suspiria-2018/",
            )
        )
        db.session.add(MovieRequest(title="Suspiria"))
        db.session.commit()

        page = self.client.get("/requests").get_data(as_text=True)
        self.assertIn("Alien", page)
        self.assertIn("Suspiria (2018)", page)
        self.assertNotIn("<h2>Suspiria</h2>", page)

    def test_admin_can_edit_and_delete_movie_requests(self):
        movie_request = MovieRequest(
            title="Alter Titel", suggester_name="Mira", status="pending"
        )
        db.session.add(movie_request)
        db.session.flush()
        db.session.add(
            MovieVote(request_id=movie_request.id, name="Leo", name_key="leo")
        )
        db.session.commit()
        with self.client.session_transaction() as session:
            session["is_admin"] = True

        response = self.client.post(
            f"/admin/requests/{movie_request.id}/edit",
            data={
                "title": "Neuer Titel",
                "suggester_name": "Miriam",
                "letterboxd_url": "",
                "poster_url": "https://example.com/poster.jpg",
                "status": "approved",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("tab=requests", response.location)
        db.session.refresh(movie_request)
        self.assertEqual(movie_request.title, "Neuer Titel")
        self.assertEqual(movie_request.suggester_name, "Miriam")
        self.assertEqual(movie_request.status, "approved")

        response = self.client.get("/admin?tab=requests")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Neuer Titel", response.data)
        self.assertIn(b"requests-tab", response.data)

        response = self.client.post(
            f"/admin/requests/{movie_request.id}/delete"
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(MovieRequest.query.count(), 0)
        self.assertEqual(MovieVote.query.count(), 0)

    def test_admin_edit_rejects_duplicate_title(self):
        first = MovieRequest(title="Alien", suggester_name="Mira")
        second = MovieRequest(title="Blade Runner", suggester_name="Leo")
        db.session.add_all([first, second])
        db.session.commit()
        with self.client.session_transaction() as session:
            session["is_admin"] = True

        response = self.client.post(
            f"/admin/requests/{second.id}/edit",
            data={
                "title": "ALIEN",
                "suggester_name": "Leo",
                "letterboxd_url": "",
                "poster_url": "",
                "status": "pending",
            },
        )
        self.assertEqual(response.status_code, 200)
        db.session.refresh(second)
        self.assertEqual(second.title, "Blade Runner")

    def test_batch_voting_uses_one_name_for_multiple_movies(self):
        first = MovieRequest(title="Alien", suggester_name="Mira")
        second = MovieRequest(
            title="Blade Runner", suggester_name="Leo", status="approved"
        )
        rejected = MovieRequest(
            title="Rejected", suggester_name="Kim", status="rejected"
        )
        db.session.add_all([first, second, rejected])
        db.session.commit()

        page = self.client.get("/requests").get_data(as_text=True)
        self.assertEqual(page.count('name="name"'), 1)
        self.assertEqual(page.count('name="request_ids"'), 2)

        response = self.client.post(
            "/requests/vote",
            data={
                "name": "Mira",
                "request_ids": [str(first.id), str(second.id), str(rejected.id)],
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(MovieVote.query.count(), 2)

        self.client.post(
            "/requests/vote",
            data={
                "name": " mira ",
                "request_ids": [str(first.id), str(second.id)],
            },
        )
        self.assertEqual(MovieVote.query.count(), 2)


if __name__ == "__main__":
    unittest.main()
