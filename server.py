print (" [+] Loading basics...")
import os
import json
import urllib
if os.name == 'nt':
    os.system("color")
    os.system("title Social Empires Server")
else:
    import sys
    sys.stdout.write("\x1b]2;Social Empires Server\x07")

print (" [+] Loading game config...")
from get_game_config import get_game_config, patch_game_config, refresh_darts_schedule

print (" [+] Loading players...")
from get_player_info import get_player_info, get_neighbor_info
from sessions import load_saved_villages, all_saves_userid, all_saves_info, save_info, new_village, fb_friends_str
from auth import has_password, set_password, check_password, change_password
load_saved_villages()

WORKING_GAMEVERSION = "SocialEmpires0926bsec.swf"

print (" [+] Loading server...")
from flask import Flask, render_template, send_from_directory, request, redirect, session
from flask.debughelpers import attach_enctype_error_multidict
from command import command
from engine import timestamp_now
from version import version_name
from constants import Constant
from quests import get_quest_map
from bundle import ASSETS_DIR, STUB_DIR, TEMPLATES_DIR, BASE_DIR

host = '127.0.0.1'
port = 5050

app = Flask(__name__, template_folder=TEMPLATES_DIR)


@app.after_request
def _no_cache_dynamic(resp):
    # The dynamic game API (player info, config, commands) carries live state
    # such as the daily-darts availability. If the browser/Ruffle caches it,
    # a reload replays stale state (e.g. an already-used darts game shows as
    # throwable again). Force revalidation on these endpoints; static assets
    # (swf/images) stay cacheable.
    if "srvempires" in request.path:
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp

print (" [+] Configuring server routes...")

##########
# ROUTES #
##########

## PAGES AND RESOURCES

@app.route("/", methods=['GET', 'POST'])
def login():
    # Log out any previous / pending session
    session.pop('USERID', default=None)
    session.pop('GAMEVERSION', default=None)
    session.pop('PENDING_USERID', default=None)
    # Reload saves. Allows saves modification without server reset
    load_saved_villages()
    if request.method == 'POST':
        USERID = request.form['USERID']
        GAMEVERSION = request.form.get('GAMEVERSION', WORKING_GAMEVERSION)
        password = request.form.get('password', '')
        if USERID not in all_saves_userid():
            return render_template("login.html", saves_info=all_saves_info(), version=version_name, error="Unknown village.")
        # Legacy village with no password yet: send the player to create one
        # before entering the game (no game session granted yet).
        if not has_password(USERID):
            session['PENDING_USERID'] = USERID
            session['GAMEVERSION'] = GAMEVERSION
            return redirect("/set-password")
        if not check_password(USERID, password):
            return render_template("login.html", saves_info=all_saves_info(), version=version_name, error="Incorrect password.")
        session['USERID'] = USERID
        session['GAMEVERSION'] = GAMEVERSION
        print("[LOGIN] USERID:", USERID)
        return redirect("/play.html")
    # Login page
    return render_template("login.html", saves_info=all_saves_info(), version=version_name)

@app.route("/set-password", methods=['GET', 'POST'])
def set_password_page():
    # Only reachable mid-flow (new village or first login of a legacy village).
    USERID = session.get('PENDING_USERID')
    load_saved_villages()
    if not USERID or USERID not in all_saves_userid():
        return redirect("/")
    name = save_info(USERID)["name"]
    if request.method == 'POST':
        pw = request.form.get('password', '')
        confirm = request.form.get('confirm', '')
        if not pw:
            return render_template("set_password.html", name=name, error="Password cannot be empty.")
        if pw != confirm:
            return render_template("set_password.html", name=name, error="Passwords do not match.")
        set_password(USERID, pw)
        # Promote the pending village to a real game session.
        session.pop('PENDING_USERID', default=None)
        session['USERID'] = USERID
        if 'GAMEVERSION' not in session:
            session['GAMEVERSION'] = WORKING_GAMEVERSION
        print("[SET-PASSWORD] USERID:", USERID)
        return redirect("/play.html")
    return render_template("set_password.html", name=name)

@app.route("/change-password", methods=['GET', 'POST'])
def change_password_page():
    load_saved_villages()
    if request.method == 'POST':
        USERID = request.form['USERID']
        old = request.form.get('current', '')
        new = request.form.get('password', '')
        confirm = request.form.get('confirm', '')
        if USERID not in all_saves_userid():
            return render_template("change_password.html", saves_info=all_saves_info(), error="Unknown village.")
        if not has_password(USERID):
            return render_template("change_password.html", saves_info=all_saves_info(), error="This village has no password yet — set one by logging in.")
        if not new:
            return render_template("change_password.html", saves_info=all_saves_info(), error="New password cannot be empty.")
        if new != confirm:
            return render_template("change_password.html", saves_info=all_saves_info(), error="New passwords do not match.")
        if not change_password(USERID, old, new):
            return render_template("change_password.html", saves_info=all_saves_info(), error="Current password is incorrect.")
        print("[CHANGE-PASSWORD] USERID:", USERID)
        return render_template("change_password.html", saves_info=all_saves_info(), message="Password changed. You can now log in.")
    return render_template("change_password.html", saves_info=all_saves_info())

@app.route("/play.html")
def play():
    print(session)

    if 'USERID' not in session:
        return redirect("/")
    if 'GAMEVERSION' not in session:
        return redirect("/")

    if session['USERID'] not in all_saves_userid():
        return redirect("/")
    
    USERID = session['USERID']
    GAMEVERSION = session['GAMEVERSION']
    print("[PLAY] USERID:", USERID)
    print("[PLAY] GAMEVERSION:", GAMEVERSION)
    return render_template("play.html", save_info=save_info(USERID), serverTime=timestamp_now(), friendsInfo=fb_friends_str(USERID), version=version_name, GAMEVERSION=GAMEVERSION, SERVERIP=host)

@app.route("/ruffle.html")
def ruffle():
    print(session)

    if 'USERID' not in session:
        return redirect("/")
    if 'GAMEVERSION' not in session:
        return redirect("/")

    if session['USERID'] not in all_saves_userid():
        return redirect("/")
    
    USERID = session['USERID']
    GAMEVERSION = session['GAMEVERSION']
    print("[RUFFLE] USERID:", USERID)
    print("[RUFFLE] GAMEVERSION:", GAMEVERSION)
    return render_template("ruffle.html", save_info=save_info(USERID), serverTime=timestamp_now(), version=version_name, GAMEVERSION=GAMEVERSION, SERVERIP=host)


@app.route("/new.html")
def new():
    # Create the village, but require a password before entering the game.
    USERID = new_village()
    session.pop('USERID', default=None)
    session['PENDING_USERID'] = USERID
    session['GAMEVERSION'] = WORKING_GAMEVERSION
    return redirect("/set-password")

@app.route("/crossdomain.xml")
def crossdomain():
    return send_from_directory(STUB_DIR, "crossdomain.xml")

@app.route("/img/<path:path>")
def images(path):
    return send_from_directory(TEMPLATES_DIR + "/img", path)

@app.route("/css/<path:path>")
def css(path):
    return send_from_directory(TEMPLATES_DIR + "/css", path)

## GAME STATIC


@app.route("/default01.static.socialpointgames.com/static/socialempires/swf/05122012_projectiles.swf")
def similar_05122012_projectiles():
    return send_from_directory(ASSETS_DIR + "/swf", "20130417_projectiles.swf")

@app.route("/default01.static.socialpointgames.com/static/socialempires/swf/05122012_magicParticles.swf")
def similar_05122012_magicParticles():
    return send_from_directory(ASSETS_DIR + "/swf", "20131010_magicParticles.swf")

@app.route("/default01.static.socialpointgames.com/static/socialempires/swf/05122012_dynamic.swf")
def similar_05122012_dynamic():
    return send_from_directory(ASSETS_DIR + "/swf", "120608_dynamic.swf")

@app.route("/default01.static.socialpointgames.com/static/socialempires/<path:path>")
def static_assets_loader(path):
    # return send_from_directory(ASSETS_DIR, path)
    if not os.path.exists(ASSETS_DIR + "/"+ path):
        # File does not exists in provided assets
        if not os.path.exists(f"{BASE_DIR}/download_assets/assets/{path}"):
            # Download file from SP's CDN if it doesn't exist

            # Make directory
            directory = os.path.dirname(f"{BASE_DIR}/download_assets/assets/{path}")
            if not os.path.exists(directory):
                os.makedirs(directory)

            # Download File
            URL = f"https://static.socialpointgames.com/static/socialempires/assets/{path}"
            try:
                response = urllib.request.urlretrieve(URL, f"{BASE_DIR}/download_assets/assets/{path}")
            except urllib.error.HTTPError:
                return ("", 404)

            print(f"====== DOWNLOADED ASSET: {URL}")
            return send_from_directory("{BASE_DIR}/download_assets/assets", path)
        else:
            # Use downloaded CDN asset
            print(f"====== USING EXTERNAL: download_assets/assets/{path}")
            return send_from_directory("{BASE_DIR}/download_assets/assets", path)
    else:
        # Use provided asset
        return send_from_directory(ASSETS_DIR, path)

## GAME DYNAMIC

@app.route("/dynamic.flash1.dev.socialpoint.es/appsfb/socialempiresdev/srvempires/track_game_status.php", methods=['POST'])
def track_game_status_response():
    status = request.values['status']
    installId = request.values['installId']
    user_id = request.values['user_id']

    print(f"track_game_status: status={status}, installId={installId}, user_id={user_id}. --", request.values)
    return ("", 200)

@app.route("/dynamic.flash1.dev.socialpoint.es/appsfb/socialempiresdev/srvempires/get_game_config.php", methods=['GET','POST'])
def get_game_config_response():
    spdebug = None

    USERID = request.values['USERID']
    user_key = request.values['user_key']
    if 'spdebug' in request.values:
        spdebug = request.values['spdebug']
    language = request.values['language']

    print(f"get_game_config: USERID: {USERID}. --", request.values)
    # Keep the daily darts playable: the bundled prize schedule ends in 2012.
    refresh_darts_schedule()
    return get_game_config()

@app.route("/dynamic.flash1.dev.socialpoint.es/appsfb/socialempiresdev/srvempires/get_player_info.php", methods=['POST'])
def get_player_info_response():

    # The acting player is whoever is logged in - never the USERID the client
    # posts, or any browser on the network could read/act as another village.
    USERID = session.get('USERID')
    if not USERID or USERID not in all_saves_userid():
        return ({"result": "error", "error": "not_logged_in"}, 403)
    if request.values.get('USERID') not in (None, USERID):
        print(f" [!] get_player_info: posted USERID {request.values['USERID']} ignored, session is {USERID}.")
    user_key = request.values['user_key']
    spdebug = request.values['spdebug'] if 'spdebug' in request.values else None
    language = request.values['language']
    neighbors = request.values['neighbors'] if 'neighbors' in request.values else None
    client_id = request.values['client_id']
    user = request.values['user'] if 'user' in request.values else None
    map = int(request.values['map']) if 'map' in request.values else None

    print(f"get_player_info: USERID: {USERID}. user: {user} --", request.values)

    # Own player - either no `user` param, or `user` is the logged-in village
    # itself (clicking "Town" to switch between your own maps sends your own
    # id). Serve it from the session save with the requested map so a second
    # town loads with the player's private state, not the neighbour view.
    if user is None or user == USERID:
        return (get_player_info(USERID, map), 200)
    # Arthur
    elif user == Constant.NEIGHBOUR_ARTHUR_GUINEVERE_1 \
    or user == Constant.NEIGHBOUR_ARTHUR_GUINEVERE_2 \
    or user == Constant.NEIGHBOUR_ARTHUR_GUINEVERE_3:
        return (get_neighbor_info(user, map), 200)
    # Quest
    elif user.startswith("100000"): # Dirty but quick
        return get_quest_map(user)
    # Neighbor
    else:
        return (get_neighbor_info(user, map), 200)

@app.route("/dynamic.flash1.dev.socialpoint.es/appsfb/socialempiresdev/srvempires/sync_error_track.php", methods=['POST'])
def sync_error_track_response():
    spdebug = None

    USERID = request.values['USERID']
    user_key = request.values['user_key']
    if 'spdebug' in request.values:
        spdebug = request.values['spdebug']
    language = request.values['language']
    error = request.values['error']
    current_failed = request.values['current_failed']
    tries = request.values['tries'] if 'tries' in request.values else None
    survival = request.values['survival']
    previous_failed = request.values['previous_failed']
    description = request.values['description']
    user_id = request.values['user_id']

    print(f"sync_error_track: USERID: {USERID}. [Error: {error}] tries: {tries}. --", request.values)
    return ("", 200)

@app.route("/null")
def flash_sync_error_response():
    sp_ref_cat = request.values['sp_ref_cat']

    if sp_ref_cat == "flash_sync_error":
        reason = "reload On Sync Error"
    elif sp_ref_cat == "flash_reload_quest":
        reason = "reload On End Quest"
    elif sp_ref_cat == "flash_reload_attack":
        reason = "reload On End Attack"

    print("flash_sync_error", reason, ". --", request.values)
    return redirect("/play.html")

@app.route("/dynamic.flash1.dev.socialpoint.es/appsfb/socialempiresdev/srvempires/command.php", methods=['POST'])
def command_response():
    spdebug = None

    # Commands only ever apply to the logged-in village; the posted USERID is
    # untrusted (any client on the network could send someone else's id).
    USERID = session.get('USERID')
    if not USERID or USERID not in all_saves_userid():
        return ({"result": "error", "error": "not_logged_in"}, 403)
    if request.values.get('USERID') not in (None, USERID):
        print(f" [!] command: posted USERID {request.values['USERID']} ignored, session is {USERID}.")
    user_key = request.values['user_key']
    if 'spdebug' in request.values:
        spdebug = request.values['spdebug']
    language = request.values['language']
    client_id = request.values['client_id']

    print(f"command: USERID: {USERID}. --", request.values)

    data_str = request.values['data']
    data_hash = data_str[:64]
    assert data_str[64] == ';'
    data_payload = data_str[65:]
    data = json.loads(data_payload)

    command(USERID, data)
    
    return ({"result": "success"}, 200)

@app.route("/dynamic.flash1.dev.socialpoint.es/appsfb/socialempiresdev/srvempires/get_continent_ranking.php")
def get_continent_ranking_response():

    USERID = request.values['USERID']
    worldChange = request.values['worldChange']
    if 'spdebug' in request.values:
        spdebug = request.values['spdebug']
    town_id = request.values['map']
    user_key = request.values['user_key']

    # TODO - stub
    response = {
        "world_id": 0,
        "continent": [
            {"posicion": 0, "nivel": 1, "user_id": 1111}, # villages/AcidCaos
            {"posicion": 1, "nivel": 0},
            {"posicion": 2, "nivel": 0},
            {"posicion": 3, "nivel": 0},
            {"posicion": 4, "nivel": 0},
            {"posicion": 5, "nivel": 0},
            {"posicion": 6, "nivel": 0},
            {"posicion": 7, "nivel": 0}
        ]
    }
    return(response)


########
# MAIN #
########

print (" [+] Running server...")

if __name__ == '__main__':
    app.secret_key = 'SECRET_KEY'
    app.run(host=host, port=port, debug=False)
