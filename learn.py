#!/usr/bin/env python3
"""Learn ranking weights from real behaviour (clicks to vendor website / call / email).

Method: position-debiased logistic regression (inverse propensity weighting) over the exact feature
vectors that were shown at impression time. Contact actions (call/email) count double a website click.
Learned weights are blended into the defaults by ranker.load_weights (max 50% influence, grows with data).

Usage: python learn.py DB.sqlite
"""
import json
import math
import sys
import sqlite3
import datetime as dt

from ranker import FEATURES, DEFAULT_W, init


def propensity(pos):
    return 1.0 / (1.0 + math.log(1 + max(pos, 0)))          # examination probability by rank position


def train(path, epochs=30, lr=0.3, l2=0.05):
    db = sqlite3.connect(path)
    init(db)
    imps = db.execute("""SELECT sid, listing_id, position, features_json FROM events
                         WHERE type='impression' AND features_json IS NOT NULL""").fetchall()
    clicks = {}
    for sid, lid, tgt in db.execute("SELECT sid, listing_id, target FROM events WHERE type='click'"):
        clicks[(sid, lid)] = max(clicks.get((sid, lid), 0), 2 if tgt in ("call", "email") else 1)
    data = []
    for sid, lid, pos, fj in imps:
        try:
            fv = json.loads(fj)
        except ValueError:
            continue
        y = clicks.get((sid, lid), 0)
        w = min(5.0, 1.0 / propensity(pos)) * (y if y else 1)
        data.append(([fv.get(k, 0.0) for k in FEATURES], 1 if y else 0, w))
    n_clicks = sum(d[1] for d in data)
    if n_clicks < 20 or len(data) < 200:
        print(f"not enough behaviour yet ({n_clicks} clicks / {len(data)} impressions); keeping defaults")
        return None
    prior = [DEFAULT_W[k] * len(FEATURES) for k in FEATURES]     # regularise toward the defaults
    w = list(prior)
    b = 0.0
    for _ in range(epochs):
        for x, y, sw in data:
            z = b + sum(wi * xi for wi, xi in zip(w, x))
            p = 1 / (1 + math.exp(-max(min(z, 30), -30)))
            g = (p - y) * sw
            for i in range(len(w)):
                w[i] -= lr * (g * x[i] + l2 * (w[i] - prior[i])) / len(data) * 50
            b -= lr * g / len(data) * 50
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    for k, wi in zip(FEATURES, w):
        db.execute("INSERT OR REPLACE INTO learned_weights(name,w,n,updated_at) VALUES (?,?,?,?)",
                   (k, wi, n_clicks, now))
    db.commit()
    print({k: round(wi, 3) for k, wi in zip(FEATURES, w)}, "clicks:", n_clicks)
    return dict(zip(FEATURES, w))


if __name__ == "__main__":
    train(sys.argv[1])
