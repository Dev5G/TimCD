#!/usr/bin/python3


# @todo logging
# @todo extra options for url like , verify=False etc.
# @todo enable https://urllib3.readthedocs.io/en/latest/user-guide.html#ssl as option?
# @todo option for interval day/6 hour/etc
# @todo on change detected, config for calling some API
# @todo fetch title into json
# https://distill.io/features
# proxy per check
#  - flask_cors, itsdangerous,MarkupSafe

import time
import os
import timeago
import flask_login
from flask_login import login_required

import threading
from threading import Event

import queue

from flask import (
    Flask,
    render_template,
    request,
    send_from_directory,
    abort,
    redirect,
    url_for,
    flash,
)

from feedgen.feed import FeedGenerator
from flask import make_response
import datetime
import pytz
from copy import deepcopy

__version__ = "0.39.4"

datastore = None

# Local
running_update_threads = []
ticker_thread = None

extra_stylesheets = []

update_q = queue.Queue()

notification_q = queue.Queue()

# Needs to be set this way because we also build and publish via pip
base_path = os.path.dirname(os.path.realpath(__file__))
app = Flask(
    __name__,
    static_url_path="{}/static".format(base_path),
    template_folder="{}/templates".format(base_path),
)

# Stop browser caching of assets
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

app.config.exit = Event()

app.config["NEW_VERSION_AVAILABLE"] = False

app.config["LOGIN_DISABLED"] = False

# app.config["EXPLAIN_TEMPLATE_LOADING"] = True

# Disables caching of the templates
app.config["TEMPLATES_AUTO_RELOAD"] = True


def init_app_secret(datastore_path):
    secret = ""

    path = "{}/secret.txt".format(datastore_path)

    try:
        with open(path, "r") as f:
            secret = f.read()

    except FileNotFoundError:
        import secrets

        with open(path, "w") as f:
            secret = secrets.token_hex(32)
            f.write(secret)

    return secret


# Remember python is by reference
# populate_form in wtfors didnt work for me. (try using a setattr() obj type on datastore.watch?)
def populate_form_from_watch(form, watch):
    for i in form.__dict__.keys():
        if i[0] != "_":
            p = getattr(form, i)
            if hasattr(p, "data") and i in watch:
                setattr(p, "data", watch[i])


# We use the whole watch object from the store/JSON so we can see if there's some related status in terms of a thread
# running or something similar.
@app.template_filter("format_last_checked_time")
def _jinja2_filter_datetime(watch_obj, format="%Y-%m-%d %H:%M:%S"):
    # Worker thread tells us which UUID it is currently processing.
    for t in running_update_threads:
        if t.current_uuid == watch_obj["uuid"]:
            return "Checking now.."

    if watch_obj["last_checked"] == 0:
        return "Not yet"

    return timeago.format(int(watch_obj["last_checked"]), time.time())


# @app.context_processor
# def timeago():
#    def _timeago(lower_time, now):
#        return timeago.format(lower_time, now)
#    return dict(timeago=_timeago)


@app.template_filter("format_timestamp_timeago")
def _jinja2_filter_datetimestamp(timestamp, format="%Y-%m-%d %H:%M:%S"):
    return timeago.format(timestamp, time.time())
    # return timeago.format(timestamp, time.time())
    # return datetime.datetime.utcfromtimestamp(timestamp).strftime(format)


class User(flask_login.UserMixin):
    id = None

    def set_password(self, password):
        return True

    def get_user(self, email="defaultuser@changedetection.io"):
        return self

    def is_authenticated(self):

        return True

    def is_active(self):
        return True

    def is_anonymous(self):
        return False

    def get_id(self):
        return str(self.id)

    def check_password(self, password):

        import hashlib
        import base64

        # Getting the values back out
        raw_salt_pass = base64.b64decode(
            datastore.data["settings"]["application"]["password"]
        )
        salt_from_storage = raw_salt_pass[:32]  # 32 is the length of the salt

        # Use the exact same setup you used to generate the key, but this time put in the password to check
        new_key = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),  # Convert the password to bytes
            salt_from_storage,
            100000,
        )
        new_key = salt_from_storage + new_key

        return new_key == raw_salt_pass

    pass


def changedetection_app(config=None, datastore_o=None):
    global datastore
    datastore = datastore_o

    # app.config.update(config or {})

    login_manager = flask_login.LoginManager(app)
    login_manager.login_view = "login"
    app.secret_key = init_app_secret(config["datastore_path"])

    # Setup cors headers to allow all domains
    # https://flask-cors.readthedocs.io/en/latest/
    #    CORS(app)

    @login_manager.user_loader
    def user_loader(email):
        user = User()
        user.get_user(email)
        return user

    @login_manager.unauthorized_handler
    def unauthorized_handler():
        # @todo validate its a URL of this host and use that
        return redirect(url_for("login", next=url_for("index")))

    @app.route("/logout")
    def logout():
        flask_login.logout_user()
        return redirect(url_for("index"))

    # https://github.com/pallets/flask/blob/93dd1709d05a1cf0e886df6223377bdab3b077fb/examples/tutorial/flaskr/__init__.py#L39
    # You can divide up the stuff like this
    @app.route("/login", methods=["GET", "POST"])
    def login():

        if not datastore.data["settings"]["application"]["password"]:
            flash("Login not required, no password enabled.", "notice")
            return redirect(url_for("index"))

        if request.method == "GET":
            output = render_template("login.html")
            return output

        user = User()
        user.id = "defaultuser@changedetection.io"

        password = request.form.get("password")

        if user.check_password(password):
            flask_login.login_user(user, remember=True)
            next = request.args.get("next")
            #            if not is_safe_url(next):
            #                return flask.abort(400)
            return redirect(next or url_for("index"))

        else:
            flash("Incorrect password", "error")

        return redirect(url_for("login"))

    @app.before_request
    def do_something_whenever_a_request_comes_in():
        # Disable password  loginif there is not one set
        app.config["LOGIN_DISABLED"] = (
            datastore.data["settings"]["application"]["password"] == False
        )

        # For the RSS path, allow access via a token
        if request.path == "/rss" and request.args.get("token"):
            app_rss_token = datastore.data["settings"]["application"][
                "rss_access_token"
            ]
            rss_url_token = request.args.get("token")
            if app_rss_token == rss_url_token:
                app.config["LOGIN_DISABLED"] = True

    @app.route("/rss", methods=["GET"])
    @login_required
    def rss():

        limit_tag = request.args.get("tag")

        # Sort by last_changed and add the uuid which is usually the key..
        sorted_watches = []

        # @todo needs a .itemsWithTag() or something
        for uuid, watch in datastore.data["watching"].items():

            if limit_tag != None:
                # Support for comma separated list of tags.
                for tag_in_watch in watch["tag"].split(","):
                    tag_in_watch = tag_in_watch.strip()
                    if tag_in_watch == limit_tag:
                        watch["uuid"] = uuid
                        sorted_watches.append(watch)

            else:
                watch["uuid"] = uuid
                sorted_watches.append(watch)

        sorted_watches.sort(key=lambda x: x["last_changed"], reverse=True)

        fg = FeedGenerator()
        fg.title("changedetection.io")
        fg.description("Feed description")
        fg.link(href="https://changedetection.io")

        for watch in sorted_watches:
            if not watch["viewed"]:
                # Re #239 - GUID needs to be individual for each event
                # @todo In the future make this a configurable link back (see work on BASE_URL https://github.com/dgtlmoon/changedetection.io/pull/228)
                guid = "{}/{}".format(watch["uuid"], watch["last_changed"])
                fe = fg.add_entry()
                fe.title(watch["url"])
                fe.link(href=watch["url"])
                fe.description(watch["url"])
                fe.guid(guid, permalink=False)
                dt = datetime.datetime.fromtimestamp(int(watch["newest_history_key"]))
                dt = dt.replace(tzinfo=pytz.UTC)
                fe.pubDate(dt)

        response = make_response(fg.rss_str())
        response.headers.set("Content-Type", "application/rss+xml")
        return response

    @app.route("/", methods=["GET"])
    @login_required
    def index():
        limit_tag = request.args.get("tag")
        pause_uuid = request.args.get("pause")

        # Redirect for the old rss path which used the /?rss=true
        if request.args.get("rss"):
            return redirect(url_for("rss", tag=limit_tag))

        if pause_uuid:
            try:
                datastore.data["watching"][pause_uuid]["paused"] ^= True
                datastore.needs_write = True

                return redirect(url_for("index", tag=limit_tag))
            except KeyError:
                pass

        # Sort by last_changed and add the uuid which is usually the key..
        sorted_watches = []
        for uuid, watch in datastore.data["watching"].items():

            if limit_tag != None:
                # Support for comma separated list of tags.
                for tag_in_watch in watch["tag"].split(","):
                    tag_in_watch = tag_in_watch.strip()
                    if tag_in_watch == limit_tag:
                        watch["uuid"] = uuid
                        sorted_watches.append(watch)

            else:
                watch["uuid"] = uuid
                sorted_watches.append(watch)

        sorted_watches.sort(key=lambda x: x["last_changed"], reverse=True)

        existing_tags = datastore.get_all_tags()

        from changedetectionio import forms

        form = forms.quickWatchForm(request.form)

        output = render_template(
            "watch-overview.html",
            form=form,
            watches=sorted_watches,
            tags=existing_tags,
            active_tag=limit_tag,
            app_rss_token=datastore.data["settings"]["application"]["rss_access_token"],
            has_unviewed=datastore.data["has_unviewed"],
        )

        return output

    @app.route("/scrub", methods=["GET", "POST"])
    @login_required
    def scrub_page():

        import re

        if request.method == "POST":
            confirmtext = request.form.get("confirmtext")
            limit_date = request.form.get("limit_date")
            limit_timestamp = 0

            # Re #149 - allow empty/0 timestamp limit
            if len(limit_date):
                try:
                    limit_date = limit_date.replace("T", " ")
                    # I noticed chrome will show '/' but actually submit '-'
                    limit_date = limit_date.replace("-", "/")
                    # In the case that :ss seconds are supplied
                    limit_date = re.sub(r"(\d\d:\d\d)(:\d\d)", "\\1", limit_date)

                    str_to_dt = datetime.datetime.strptime(limit_date, "%Y/%m/%d %H:%M")
                    limit_timestamp = int(str_to_dt.timestamp())

                    if limit_timestamp > time.time():
                        flash("Timestamp is in the future, cannot continue.", "error")
                        return redirect(url_for("scrub_page"))

                except ValueError:
                    flash("Incorrect date format, cannot continue.", "error")
                    return redirect(url_for("scrub_page"))

            if confirmtext == "scrub":
                changes_removed = 0
                for uuid, watch in datastore.data["watching"].items():
                    if limit_timestamp:
                        changes_removed += datastore.scrub_watch(
                            uuid, limit_timestamp=limit_timestamp
                        )
                    else:
                        changes_removed += datastore.scrub_watch(uuid)

                flash(
                    "Cleared snapshot history ({} snapshots removed)".format(
                        changes_removed
                    )
                )
            else:
                flash("Incorrect confirmation text.", "error")

            return redirect(url_for("index"))

        output = render_template("scrub.html")
        return output

    # If they edited an existing watch, we need to know to reset the current/previous md5 to include
    # the excluded text.
    def get_current_checksum_include_ignore_text(uuid):

        import hashlib
        from changedetectionio import fetch_site_status

        # Get the most recent one
        newest_history_key = datastore.get_val(uuid, "newest_history_key")

        # 0 means that theres only one, so that there should be no 'unviewed' history availabe
        if newest_history_key == 0:
            newest_history_key = list(
                datastore.data["watching"][uuid]["history"].keys()
            )[0]

        if newest_history_key:
            with open(
                datastore.data["watching"][uuid]["history"][newest_history_key],
                encoding="utf-8",
            ) as file:
                raw_content = file.read()

                handler = fetch_site_status.perform_site_check(datastore=datastore)
                stripped_content = handler.strip_ignore_text(
                    raw_content, datastore.data["watching"][uuid]["ignore_text"]
                )

                checksum = hashlib.md5(stripped_content).hexdigest()
                return checksum

        return datastore.data["watching"][uuid]["previous_md5"]

    @app.route("/edit/<string:uuid>", methods=["GET", "POST"])
    @login_required
    def edit_page(uuid):
        from changedetectionio import forms

        form = forms.watchForm(request.form)

        # More for testing, possible to return the first/only
        if uuid == "first":
            uuid = list(datastore.data["watching"].keys()).pop()

        if request.method == "GET":
            if not uuid in datastore.data["watching"]:
                flash("No watch with the UUID %s found." % (uuid), "error")
                return redirect(url_for("index"))

            populate_form_from_watch(form, datastore.data["watching"][uuid])

            if datastore.data["watching"][uuid]["fetch_backend"] is None:
                form.fetch_backend.data = datastore.data["settings"]["application"][
                    "fetch_backend"
                ]

        if request.method == "POST" and form.validate():

            # Re #110, if they submit the same as the default value, set it to None, so we continue to follow the default
            if (
                form.minutes_between_check.data
                == datastore.data["settings"]["requests"]["minutes_between_check"]
            ):
                form.minutes_between_check.data = None

            if (
                form.fetch_backend.data
                == datastore.data["settings"]["application"]["fetch_backend"]
            ):
                form.fetch_backend.data = None

            update_obj = {
                "url": form.url.data.strip(),
                "minutes_between_check": form.minutes_between_check.data,
                "tag": form.tag.data.strip(),
                "title": form.title.data.strip(),
                "headers": form.headers.data,
                "fetch_backend": form.fetch_backend.data,
                "trigger_text": form.trigger_text.data,
                "notification_title": form.notification_title.data,
                "notification_body": form.notification_body.data,
                "notification_format": form.notification_format.data,
                "extract_title_as_title": form.extract_title_as_title.data,
            }

            # Notification URLs
            datastore.data["watching"][uuid][
                "notification_urls"
            ] = form.notification_urls.data

            # Ignore text
            form_ignore_text = form.ignore_text.data
            datastore.data["watching"][uuid]["ignore_text"] = form_ignore_text

            # Reset the previous_md5 so we process a new snapshot including stripping ignore text.
            if form_ignore_text:
                if len(datastore.data["watching"][uuid]["history"]):
                    update_obj[
                        "previous_md5"
                    ] = get_current_checksum_include_ignore_text(uuid=uuid)

            datastore.data["watching"][uuid][
                "css_filter"
            ] = form.css_filter.data.strip()

            # Reset the previous_md5 so we process a new snapshot including stripping ignore text.
            if (
                form.css_filter.data.strip()
                != datastore.data["watching"][uuid]["css_filter"]
            ):
                if len(datastore.data["watching"][uuid]["history"]):
                    update_obj[
                        "previous_md5"
                    ] = get_current_checksum_include_ignore_text(uuid=uuid)

            datastore.data["watching"][uuid].update(update_obj)

            flash("Updated watch.")

            # Re #286 - We wait for syncing new data to disk in another thread every 60 seconds
            # But in the case something is added we should save straight away
            datastore.sync_to_json()

            # Queue the watch for immediate recheck
            update_q.put(uuid)

            if form.trigger_check.data:
                if len(form.notification_urls.data):
                    n_object = {
                        "watch_url": form.url.data.strip(),
                        "notification_urls": form.notification_urls.data,
                        "notification_title": form.notification_title.data,
                        "notification_body": form.notification_body.data,
                        "notification_format": form.notification_format.data,
                    }
                    notification_q.put(n_object)
                    flash("Test notification queued.")
                else:
                    flash("No notification URLs set, cannot send test.", "error")

            # Diff page [edit] link should go back to diff page
            if request.args.get("next") and request.args.get("next") == "diff":
                return redirect(url_for("diff_history_page", uuid=uuid))
            else:
                return redirect(url_for("index"))

        else:
            if request.method == "POST" and not form.validate():
                flash("An error occurred, please see below.", "error")

            # Re #110 offer the default minutes
            using_default_minutes = False
            if form.minutes_between_check.data == None:
                form.minutes_between_check.data = datastore.data["settings"][
                    "requests"
                ]["minutes_between_check"]
                using_default_minutes = True

            output = render_template(
                "edit.html",
                uuid=uuid,
                watch=datastore.data["watching"][uuid],
                form=form,
                using_default_minutes=using_default_minutes,
                current_base_url=datastore.data["settings"]["application"]["base_url"],
            )

        return output

    @app.route("/settings", methods=["GET", "POST"])
    @login_required
    def settings_page():

        from changedetectionio import forms
        from changedetectionio import content_fetcher


        form = forms.globalSettingsForm(request.form)

        proxies_count = (
            len(datastore.data["settings"]["application"]["proxies"])
            if len(datastore.data["settings"]["application"]["proxies"])
            else 0
        )
        bad_proxies_count = (
            len(datastore.data["settings"]["application"]["bad_proxies"])
            if len(datastore.data["settings"]["application"]["bad_proxies"])
            else 0
        )
        if request.method == "GET":
            from changedetectionio import proxy
            form.proxies.data = proxy.create_proxy_output_with_linebreaks(datastore.data["settings"]["application"]["proxies"])
            form.use_proxy.data = datastore.data["settings"]["application"]["use_proxy"]
            form.bad_proxies.data = datastore.data["settings"]["application"]["bad_proxies"]
            form.minutes_between_check.data = int(
                datastore.data["settings"]["requests"]["minutes_between_check"]
            )
            form.notification_urls.data = datastore.data["settings"]["application"][
                "notification_urls"
            ]

            form.extract_title_as_title.data = datastore.data["settings"][
                "application"
            ]["extract_title_as_title"]
            form.fetch_backend.data = datastore.data["settings"]["application"][
                "fetch_backend"
            ]
            form.notification_title.data = datastore.data["settings"]["application"][
                "notification_title"
            ]
            form.notification_body.data = datastore.data["settings"]["application"][
                "notification_body"
            ]
            form.notification_format.data = datastore.data["settings"]["application"][
                "notification_format"
            ]
            form.base_url.data = datastore.data["settings"]["application"]["base_url"]

            # Password unset is a GET
            if request.values.get("removepassword") == "yes":
                from pathlib import Path

                datastore.data["settings"]["application"]["password"] = False
                flash("Password protection removed.", "notice")
                flask_login.logout_user()
                return redirect(url_for("settings_page"))

        if request.method == "POST" and form.validate():
            from changedetectionio import proxy
            datastore.data["settings"]["application"]["proxies"] = proxy.create_proxy_list(form.proxies.data)
            datastore.data["settings"]["application"]["use_proxy"] = form.use_proxy.data
            datastore.data["settings"]["requests"][
                "minutes_between_check"
            ] = form.minutes_between_check.data
            datastore.data["settings"]["application"][
                "extract_title_as_title"
            ] = form.extract_title_as_title.data
            datastore.data["settings"]["application"][
                "fetch_backend"
            ] = form.fetch_backend.data
            datastore.data["settings"]["application"][
                "notification_title"
            ] = form.notification_title.data
            datastore.data["settings"]["application"][
                "notification_body"
            ] = form.notification_body.data
            datastore.data["settings"]["application"][
                "notification_format"
            ] = form.notification_format.data
            datastore.data["settings"]["application"][
                "notification_urls"
            ] = form.notification_urls.data
            datastore.data["settings"]["application"]["base_url"] = form.base_url.data

            if form.trigger_check.data:
                if len(form.notification_urls.data):
                    n_object = {
                        "watch_url": "Test from changedetection.io!",
                        "notification_urls": form.notification_urls.data,
                        "notification_title": form.notification_title.data,
                        "notification_body": form.notification_body.data,
                        "notification_format": form.notification_format.data,
                    }
                    notification_q.put(n_object)
                    flash("Test notification queued.")
                else:
                    flash("No notification URLs set, cannot send test.", "error")

            if form.password.encrypted_password:
                datastore.data["settings"]["application"][
                    "password"
                ] = form.password.encrypted_password
                flash("Password protection enabled.", "notice")
                flask_login.logout_user()
                return redirect(url_for("index"))

            datastore.needs_write = True
            flash("Settings updated.")

        if request.method == "POST" and not form.validate():
            flash("An error occurred, please see below.", "error")

        output = render_template(
            "settings.html",
            form=form,
            proxies_count=proxies_count,
            bad_proxies_count=bad_proxies_count,
            current_base_url=datastore.data["settings"]["application"]["base_url"],
        )

        return output

    @app.route("/import", methods=["GET", "POST"])
    @login_required
    def import_page():
        import validators

        remaining_urls = []

        good = 0

        if request.method == "POST":
            urls = request.values.get("urls").split("\n")
            for url in urls:
                url = url.strip()
                if len(url) and validators.url(url):
                    new_uuid = datastore.add_watch(url=url.strip(), tag="")
                    # Straight into the queue.
                    update_q.put(new_uuid)
                    good += 1
                else:
                    if len(url):
                        remaining_urls.append(url)

            flash("{} Imported, {} Skipped.".format(good, len(remaining_urls)))

            if len(remaining_urls) == 0:
                # Looking good, redirect to index.
                return redirect(url_for("index"))

        # Could be some remaining, or we could be on GET
        output = render_template("import.html", remaining="\n".join(remaining_urls))
        return output

    # Clear all statuses, so we do not see the 'unviewed' class
    @app.route("/api/mark-all-viewed", methods=["GET"])
    @login_required
    def mark_all_viewed():

        # Save the current newest history as the most recently viewed
        for watch_uuid, watch in datastore.data["watching"].items():
            datastore.set_last_viewed(watch_uuid, watch["newest_history_key"])

        flash("Cleared all statuses.")
        return redirect(url_for("index"))

    @app.route("/diff/<string:uuid>", methods=["GET"])
    @login_required
    def diff_history_page(uuid):

        # More for testing, possible to return the first/only
        if uuid == "first":
            uuid = list(datastore.data["watching"].keys()).pop()

        extra_stylesheets = [
            url_for("static_content", group="styles", filename="diff.css")
        ]
        try:
            watch = datastore.data["watching"][uuid]
        except KeyError:
            flash("No history found for the specified link, bad link?", "error")
            return redirect(url_for("index"))

        dates = list(watch["history"].keys())
        # Convert to int, sort and back to str again
        # @todo replace datastore getter that does this automatically
        dates = [int(i) for i in dates]
        dates.sort(reverse=True)
        dates = [str(i) for i in dates]

        if len(dates) < 2:
            flash(
                "Not enough saved change detection snapshots to produce a report.",
                "error",
            )
            return redirect(url_for("index"))

        # Save the current newest history as the most recently viewed
        datastore.set_last_viewed(uuid, dates[0])
        newest_file = watch["history"][dates[0]]
        with open(newest_file, "r") as f:
            newest_version_file_contents = f.read()

        previous_version = request.args.get("previous_version")
        try:
            previous_file = watch["history"][previous_version]
        except KeyError:
            # Not present, use a default value, the second one in the sorted list.
            previous_file = watch["history"][dates[1]]

        with open(previous_file, "r") as f:
            previous_version_file_contents = f.read()

        output = render_template(
            "diff.html",
            watch_a=watch,
            newest=newest_version_file_contents,
            previous=previous_version_file_contents,
            extra_stylesheets=extra_stylesheets,
            versions=dates[1:],
            uuid=uuid,
            newest_version_timestamp=dates[0],
            current_previous_version=str(previous_version),
            current_diff_url=watch["url"],
            extra_title=" - Diff - {}".format(
                watch["title"] if watch["title"] else watch["url"]
            ),
            left_sticky=True,
        )

        return output

    @app.route("/preview/<string:uuid>", methods=["GET"])
    @login_required
    def preview_page(uuid):

        # More for testing, possible to return the first/only
        if uuid == "first":
            uuid = list(datastore.data["watching"].keys()).pop()

        extra_stylesheets = [
            url_for("static_content", group="styles", filename="diff.css")
        ]

        try:
            watch = datastore.data["watching"][uuid]
        except KeyError:
            flash("No history found for the specified link, bad link?", "error")
            return redirect(url_for("index"))

        newest = list(watch["history"].keys())[-1]
        with open(watch["history"][newest], "r") as f:
            content = f.readlines()

        output = render_template(
            "preview.html",
            content=content,
            extra_stylesheets=extra_stylesheets,
            current_diff_url=watch["url"],
            uuid=uuid,
        )
        return output

    @app.route("/favicon.ico", methods=["GET"])
    def favicon():
        return send_from_directory("static/images", path="favicon.ico")

    # We're good but backups are even better!
    @app.route("/backup", methods=["GET"])
    @login_required
    def get_backup():

        import zipfile
        from pathlib import Path

        # Remove any existing backup file, for now we just keep one file
        for previous_backup_filename in Path(app.config["datastore_path"]).rglob(
            "changedetection-backup-*.zip"
        ):
            os.unlink(previous_backup_filename)

        # create a ZipFile object
        backupname = "changedetection-backup-{}.zip".format(int(time.time()))

        # We only care about UUIDS from the current index file
        uuids = list(datastore.data["watching"].keys())
        backup_filepath = os.path.join(app.config["datastore_path"], backupname)

        with zipfile.ZipFile(
            backup_filepath, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=8
        ) as zipObj:

            # Be sure we're written fresh
            datastore.sync_to_json()

            # Add the index
            zipObj.write(
                os.path.join(app.config["datastore_path"], "url-watches.json"),
                arcname="url-watches.json",
            )

            # Add the flask app secret
            zipObj.write(
                os.path.join(app.config["datastore_path"], "secret.txt"),
                arcname="secret.txt",
            )

            # Add any snapshot data we find, use the full path to access the file, but make the file 'relative' in the Zip.
            for txt_file_path in Path(app.config["datastore_path"]).rglob("*.txt"):
                parent_p = txt_file_path.parent
                if parent_p.name in uuids:
                    zipObj.write(
                        txt_file_path,
                        arcname=str(txt_file_path).replace(
                            app.config["datastore_path"], ""
                        ),
                        compress_type=zipfile.ZIP_DEFLATED,
                        compresslevel=8,
                    )

            # Create a list file with just the URLs, so it's easier to port somewhere else in the future
            list_file = os.path.join(app.config["datastore_path"], "url-list.txt")
            with open(list_file, "w") as f:
                for uuid in datastore.data["watching"]:
                    url = datastore.data["watching"][uuid]["url"]
                    f.write("{}\r\n".format(url))

            # Add it to the Zip
            zipObj.write(
                list_file,
                arcname="url-list.txt",
                compress_type=zipfile.ZIP_DEFLATED,
                compresslevel=8,
            )

        return send_from_directory(
            app.config["datastore_path"], backupname, as_attachment=True
        )

    @app.route("/static/<string:group>/<string:filename>", methods=["GET"])
    def static_content(group, filename):
        # These files should be in our subdirectory
        try:
            return send_from_directory("static/{}".format(group), path=filename)
        except FileNotFoundError:
            abort(404)

    @app.route("/api/add", methods=["POST"])
    @login_required
    def api_watch_add():
        from changedetectionio import forms

        form = forms.quickWatchForm(request.form)

        if form.validate():

            url = request.form.get("url").strip()
            if datastore.url_exists(url):
                flash("The URL {} already exists".format(url), "error")
                return redirect(url_for("index"))

            # @todo add_watch should throw a custom Exception for validation etc
            new_uuid = datastore.add_watch(url=url, tag=request.form.get("tag").strip())
            # Straight into the queue.
            update_q.put(new_uuid)

            flash("Watch added.")
            return redirect(url_for("index"))
        else:
            flash("Error")
            return redirect(url_for("index"))

    @app.route("/api/delete", methods=["GET"])
    @login_required
    def api_delete():

        uuid = request.args.get("uuid")
        datastore.delete(uuid)
        flash("Deleted.")

        return redirect(url_for("index"))

    @app.route("/api/clone", methods=["GET"])
    @login_required
    def api_clone():
        uuid = request.args.get("uuid")
        # More for testing, possible to return the first/only
        if uuid == "first":
            uuid = list(datastore.data["watching"].keys()).pop()

        new_uuid = datastore.clone(uuid)
        update_q.put(new_uuid)
        flash("Cloned.")

        return redirect(url_for("index"))

    @app.route("/api/checknow", methods=["GET"])
    @login_required
    def api_watch_checknow():

        tag = request.args.get("tag")
        uuid = request.args.get("uuid")
        i = 0

        running_uuids = []
        for t in running_update_threads:
            running_uuids.append(t.current_uuid)

        # @todo check thread is running and skip

        if uuid:
            if uuid not in running_uuids:
                update_q.put(uuid)
            i = 1

        elif tag != None:
            # Items that have this current tag
            for watch_uuid, watch in datastore.data["watching"].items():
                if tag != None and tag in watch["tag"]:
                    if (
                        watch_uuid not in running_uuids
                        and not datastore.data["watching"][watch_uuid]["paused"]
                    ):
                        update_q.put(watch_uuid)
                        i += 1

        else:
            # No tag, no uuid, add everything.
            for watch_uuid, watch in datastore.data["watching"].items():

                if (
                    watch_uuid not in running_uuids
                    and not datastore.data["watching"][watch_uuid]["paused"]
                ):
                    update_q.put(watch_uuid)
                    i += 1
        flash("{} watches are rechecking.".format(i))
        return redirect(url_for("index", tag=tag))

    # @todo handle ctrl break
    ticker_thread = threading.Thread(
        target=ticker_thread_check_time_launch_checks
    ).start()

    threading.Thread(target=notification_runner).start()

    # Check for new release version, but not when running in test/build
    if not os.getenv("GITHUB_REF", False):
        threading.Thread(target=check_for_new_version).start()

    return app


# Check for new version and anonymous stats
def check_for_new_version():
    import requests

    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    while not app.config.exit.is_set():
        try:
            r = requests.post(
                "https://changedetection.io/check-ver.php",
                data={
                    "version": __version__,
                    "app_guid": datastore.data["app_guid"],
                    "watch_count": len(datastore.data["watching"]),
                },
                verify=False,
            )
        except:
            pass

        try:
            if "new_version" in r.text:
                app.config["NEW_VERSION_AVAILABLE"] = True
        except:
            pass

        # Check daily
        app.config.exit.wait(86400)


def notification_runner():
    while not app.config.exit.is_set():
        try:
            # At the moment only one thread runs (single runner)
            n_object = notification_q.get(block=False)
        except queue.Empty:
            time.sleep(1)

        else:
            # Process notifications
            try:
                from changedetectionio import notification

                notification.process_notification(n_object, datastore)

            except Exception as e:
                print("Watch URL: {}  Error {}".format(n_object["watch_url"], e))


# Thread runner to check every minute, look for new watches to feed into the Queue.
def ticker_thread_check_time_launch_checks():
    from changedetectionio import update_worker
    import logging

    # Spin up Workers.
    for _ in range(datastore.data["settings"]["requests"]["workers"]):
        new_worker = update_worker.update_worker(
            update_q, notification_q, app, datastore
        )
        running_update_threads.append(new_worker)
        new_worker.start()

    while not app.config.exit.is_set():

        # Get a list of watches by UUID that are currently fetching data
        running_uuids = []
        for t in running_update_threads:
            if t.current_uuid:
                running_uuids.append(t.current_uuid)

        # Re #232 - Deepcopy the data incase it changes while we're iterating through it all
        copied_datastore = deepcopy(datastore)

        # Check for watches outside of the time threshold to put in the thread queue.
        for uuid, watch in copied_datastore.data["watching"].items():
            # If they supplied an individual entry minutes to threshold.
            if (
                "minutes_between_check" in watch
                and watch["minutes_between_check"] is not None
            ):
                # Cast to int just incase
                max_time = int(
                    watch["minutes_between_check"]
                )  # multiply by *60 to convert to minutes
            else:
                # Default system wide.
                max_time = int(
                    copied_datastore.data["settings"]["requests"][
                        "minutes_between_check"
                    ]
                )  # multiply by *60 to convert to minutes

            threshold = time.time() - max_time
            logging.info("#timecheck", max_time, "now time", threshold)
            # Yeah, put it in the queue, it's more than time.
            if not watch["paused"] and watch["last_checked"] <= threshold:
                if not uuid in running_uuids and uuid not in update_q.queue:
                    update_q.put(uuid)

        # Wait a few seconds before checking the list again
        time.sleep(3)

        # Should be low so we can break this out in testing
        app.config.exit.wait(1)
