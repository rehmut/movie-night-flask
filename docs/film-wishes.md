# Film wishes

Suggestions without a Letterboxd link search by title. Ambiguous matches require
selection; adding the release year helps. Names are required for suggestions and
votes. A normalized name can vote once per film, but names are not authenticated.
Duplicate titles and known Letterboxd film URLs are rejected, including concurrent
submissions of newly suggested films through unique identity records.

To fill missing posters or replace Letterboxd backdrops on existing suggestions,
run this command in the server environment with its configured DATABASE_URL:

```sh
python -m flask --app app refresh-request-posters
```

Ambiguous matches are skipped for manual review. Requests use curl_cffi and load
Letterboxd's separate search-result fragment. Temporary blocks remain possible;
failed lookups do not discard suggestions. No replacement graphics are shown.
Existing duplicate records are preserved for manual review.

Films whose event start time is in the past disappear from public and admin wish
lists and cannot receive new wishes or votes. Existing records and votes are kept.
Matching uses the Letterboxd film path, or the normalized title and release year
when a link is missing. Future events and distinct remakes remain eligible.

```sh
python -B -m unittest discover -s tests -v
```
