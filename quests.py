import json
import os

from bundle import QUESTS_DIR

def get_quest_map(questid):
    file = os.path.join(QUESTS_DIR, str(questid) + ".json")
    if not os.path.exists(file):
        return("", 404)
    with open(file, 'r') as f:
        d = json.load(f)
    return(d, 200)
