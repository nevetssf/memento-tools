#!/usr/bin/env python3
"""faces.py — local face recognition over the journal photo archive.

Embeddings via InsightFace (buffalo_l / ArcFace), stored in faces.db.
Match-only design: strangers are never named, only enrolled people match.

Subcommands:
  scan [--dir DIR]        detect+embed faces in new photos (default: journal photos)
  cluster                 cluster unlabeled faces, write contact sheets to faces-review/
  label <cluster> <name>  assign a person name to every face in a cluster
  ignore <cluster>        mark a cluster as not-a-person-of-interest
  identify <image>        who is in this photo?
  people                  list enrolled people and face counts
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from config import FACES_DB_PATH, FACES_REVIEW_DIR, JOURNAL_DIR

DB_PATH = FACES_DB_PATH
PHOTO_DIRS = sorted(JOURNAL_DIR.glob("*/photos"))
REVIEW_DIR = FACES_REVIEW_DIR
MATCH_THRESHOLD = 0.45  # cosine similarity; conservative to avoid false tags
IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

_app = None


def get_app():
    global _app
    if _app is None:
        from insightface.app import FaceAnalysis
        _app = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
        _app.prepare(ctx_id=-1, det_size=(640, 640))
    return _app


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS people (
        id INTEGER PRIMARY KEY,
        name TEXT UNIQUE NOT NULL,
        people_db_id INTEGER
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS faces (
        id INTEGER PRIMARY KEY,
        photo_path TEXT NOT NULL,
        bbox TEXT NOT NULL,
        det_score REAL,
        embedding BLOB NOT NULL,
        person_id INTEGER REFERENCES people(id),
        cluster_id INTEGER,
        ignored INTEGER DEFAULT 0,
        created TEXT DEFAULT (datetime('now'))
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_faces_photo ON faces(photo_path)")
    return conn


def emb_to_blob(e):
    return np.asarray(e, dtype=np.float32).tobytes()


def blob_to_emb(b):
    return np.frombuffer(b, dtype=np.float32)


def normed(e):
    return e / (np.linalg.norm(e) + 1e-10)


def cmd_scan(args):
    conn = db()
    seen = {r[0] for r in conn.execute("SELECT DISTINCT photo_path FROM faces")}
    # a photo with zero faces still gets a marker row? no — track in a table
    conn.execute("CREATE TABLE IF NOT EXISTS scanned (photo_path TEXT PRIMARY KEY)")
    scanned = {r[0] for r in conn.execute("SELECT photo_path FROM scanned")}

    dirs = [Path(args.dir)] if args.dir else PHOTO_DIRS
    photos = []
    for d in dirs:
        photos += [p for p in sorted(d.iterdir())
                   if p.suffix.lower() in IMG_EXTS and str(p) not in scanned]
    if not photos:
        print("Nothing new to scan.")
        return

    import cv2
    app = get_app()
    total_faces = 0
    for i, p in enumerate(photos, 1):
        img = cv2.imread(str(p))
        if img is None:
            print(f"[{i}/{len(photos)}] unreadable: {p.name}")
            conn.execute("INSERT OR IGNORE INTO scanned VALUES (?)", (str(p),))
            conn.commit()
            continue
        faces = app.get(img)
        for f in faces:
            conn.execute(
                "INSERT INTO faces (photo_path, bbox, det_score, embedding) VALUES (?,?,?,?)",
                (str(p), json.dumps([float(x) for x in f.bbox]),
                 float(f.det_score), emb_to_blob(f.normed_embedding)),
            )
        conn.execute("INSERT OR IGNORE INTO scanned VALUES (?)", (str(p),))
        conn.commit()
        total_faces += len(faces)
        print(f"[{i}/{len(photos)}] {p.name}: {len(faces)} face(s)")
    print(f"Done. {len(photos)} photos scanned, {total_faces} faces embedded.")


def cmd_cluster(args):
    from sklearn.cluster import AgglomerativeClustering
    import cv2

    conn = db()
    rows = conn.execute(
        "SELECT id, photo_path, bbox, embedding FROM faces "
        "WHERE person_id IS NULL AND ignored=0"
    ).fetchall()
    if not rows:
        print("No unlabeled faces.")
        return
    X = np.stack([normed(blob_to_emb(r[3])) for r in rows])
    if len(rows) == 1:
        labels = np.array([0])
    else:
        clustering = AgglomerativeClustering(
            n_clusters=None,  # pyright: ignore[reportArgumentType]  # sklearn hints say int, but None is required with distance_threshold
            metric="cosine", linkage="average",
            distance_threshold=0.55,
        )
        labels = clustering.fit_predict(X)

    # order clusters by size, renumber from 1
    order = {}
    for lab in labels:
        order[lab] = order.get(lab, 0) + 1
    remap = {old: i + 1 for i, (old, _) in
             enumerate(sorted(order.items(), key=lambda kv: -kv[1]))}

    for (fid, *_), lab in zip(rows, labels):
        conn.execute("UPDATE faces SET cluster_id=? WHERE id=?", (remap[lab], fid))
    conn.commit()

    # contact sheets
    REVIEW_DIR.mkdir(exist_ok=True)
    for f in REVIEW_DIR.glob("cluster-*.jpg"):
        f.unlink()
    by_cluster = {}
    for (fid, path, bbox, _), lab in zip(rows, labels):
        by_cluster.setdefault(remap[lab], []).append((fid, path, json.loads(bbox)))

    THUMB, COLS = 160, 6
    for cid, members in sorted(by_cluster.items()):
        crops = []
        for fid, path, bb in members[:24]:
            img = cv2.imread(path)
            if img is None:
                continue
            x1, y1, x2, y2 = [int(v) for v in bb]
            h, w = img.shape[:2]
            mx, my = int((x2 - x1) * 0.25), int((y2 - y1) * 0.25)
            crop = img[max(0, y1 - my):min(h, y2 + my), max(0, x1 - mx):min(w, x2 + mx)]
            if crop.size == 0:
                continue
            crops.append(cv2.resize(crop, (THUMB, THUMB)))
        if not crops:
            continue
        rows_n = (len(crops) + COLS - 1) // COLS
        sheet = np.zeros((rows_n * THUMB, COLS * THUMB, 3), dtype=np.uint8)
        for i, c in enumerate(crops):
            r, col = divmod(i, COLS)
            sheet[r * THUMB:(r + 1) * THUMB, col * THUMB:(col + 1) * THUMB] = c
        out = REVIEW_DIR / f"cluster-{cid:02d}.jpg"
        cv2.imwrite(str(out), sheet)
        print(f"cluster {cid}: {len(members)} faces -> {out}")


def cmd_label(args):
    conn = db()
    cur = conn.execute("SELECT id FROM people WHERE name=?", (args.name,)).fetchone()
    pid = cur[0] if cur else conn.execute(
        "INSERT INTO people (name, people_db_id) VALUES (?,?)",
        (args.name, args.people_db_id)).lastrowid
    n = conn.execute(
        "UPDATE faces SET person_id=?, cluster_id=NULL WHERE cluster_id=? "
        "AND person_id IS NULL", (pid, args.cluster)).rowcount
    conn.commit()
    print(f"Labeled {n} faces in cluster {args.cluster} as {args.name} (person id {pid}).")


def cmd_ignore(args):
    conn = db()
    n = conn.execute(
        "UPDATE faces SET ignored=1, cluster_id=NULL WHERE cluster_id=?",
        (args.cluster,)).rowcount
    conn.commit()
    print(f"Ignored {n} faces in cluster {args.cluster}.")


def cmd_identify(args):
    import cv2
    conn = db()
    known = conn.execute(
        "SELECT f.embedding, p.name FROM faces f JOIN people p ON f.person_id=p.id"
    ).fetchall()
    if not known:
        print("No enrolled people yet — run cluster + label first.")
        return
    K = np.stack([normed(blob_to_emb(e)) for e, _ in known])
    names = [n for _, n in known]

    img = cv2.imread(args.image)
    if img is None:
        sys.exit(f"Cannot read {args.image}")
    results = []
    for f in get_app().get(img):
        sims = K @ normed(f.normed_embedding)
        best = int(np.argmax(sims))
        if sims[best] >= MATCH_THRESHOLD:
            results.append({"name": names[best], "similarity": round(float(sims[best]), 3)})
        else:
            results.append({"name": None, "similarity": round(float(sims[best]), 3)})
    print(json.dumps({"faces": results}, indent=2))


def cmd_people(args):
    conn = db()
    for name, cnt in conn.execute(
        "SELECT p.name, COUNT(f.id) AS cnt FROM people p LEFT JOIN faces f "
        "ON f.person_id=p.id GROUP BY p.id ORDER BY cnt DESC"):
        print(f"{name}: {cnt} faces")
    row = conn.execute(
        "SELECT COUNT(*) FROM faces WHERE person_id IS NULL AND ignored=0").fetchone()
    print(f"(unlabeled: {row[0]})")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan"); s.add_argument("--dir"); s.set_defaults(fn=cmd_scan)
    s = sub.add_parser("cluster"); s.set_defaults(fn=cmd_cluster)
    s = sub.add_parser("label")
    s.add_argument("cluster", type=int); s.add_argument("name")
    s.add_argument("--people-db-id", type=int); s.set_defaults(fn=cmd_label)
    s = sub.add_parser("ignore"); s.add_argument("cluster", type=int); s.set_defaults(fn=cmd_ignore)
    s = sub.add_parser("identify"); s.add_argument("image"); s.set_defaults(fn=cmd_identify)
    s = sub.add_parser("people"); s.set_defaults(fn=cmd_people)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
