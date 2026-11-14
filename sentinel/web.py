"""Webserver (nur Standardbibliothek) mit JSON-API, Live-MJPEG und statischen Dateien."""
import json
import mimetypes
import os
import re
import threading
import time
import uuid
import socketserver
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from .detector import CLASS_GROUP, COCO, DE, GROUPS
from .engine import track_key

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")


def det_json(engine, r, labels=None):
    g = CLASS_GROUP.get(r["cls"])
    oid = r.get("object_id")
    lab = None
    if labels is not None and oid in labels:
        lab = labels[oid]
    return dict(id=r["id"], ts=r["ts"], stream_id=r["stream_id"], stream=r["stream_name"], cls=r["cls"],
                cls_de=DE.get(r["cls"], r["cls"]), color=g["color"] if g else "#9aa4b2",
                group=g["name"] if g else "", score=round(r["score"] or 0, 2),
                box=[r["x1"], r["y1"], r["x2"], r["y2"]], object_id=oid,
                object=lab["label"] if lab else None, named=bool(lab and lab["named"]),
                category=lab["cat"] if lab else None, cat_color=lab["ccol"] if lab else None,
                face=bool(r["face"]), match=r["match"] or "", sim=round(r["sim"] or 0, 3),
                sighting=bool(r["sighting"]), manual=bool(r["manual"]), track=r["track"],
                img="/files/" + r["img"] if r["img"] else None, crop="/files/" + r["crop"] if r["crop"] else None)


def obj_labels(engine, oids):
    oids = [int(o) for o in set(oids) if o]
    if not oids:
        return {}
    out = {}
    for o in engine.store.q("SELECT o.id, o.cls, o.name, o.seq, c.name cat, c.color ccol FROM objects o LEFT JOIN "
                            "categories c ON c.id=o.category_id WHERE o.id IN (%s)" % ",".join(map(str, oids))):
        out[o["id"]] = dict(label=o["name"] or "%s %d" % (DE.get(o["cls"], o["cls"]), o["seq"] or o["id"]),
                            named=bool(o["name"]), cat=o["cat"], ccol=o["ccol"])
    return out


def obj_json(o):
    g = CLASS_GROUP.get(o["cls"])
    return dict(id=o["id"], cls=o["cls"], cls_de=DE.get(o["cls"], o["cls"]), name=o["name"] or "",
                label=o["name"] or "%s %d" % (DE.get(o["cls"], o["cls"]), o["seq"] or o["id"]),
                color=g["color"] if g else "#9aa4b2", group=g["id"] if g else "other",
                category_id=o["category_id"], category=o.get("cat"), cat_color=o.get("ccol"),
                notes=o.get("notes") or "", sightings=o["sightings"], detections=o["detections"],
                first_seen=o["first_seen"], last_seen=o["last_seen"], has_face=bool(o["has_face"]),
                streams=[s for s in (o.get("streams") or "").split("\x1f") if s],
                avatar="/avatar/%d.png?v=%s" % (o["id"], o["avatar_det"] or 0) if o["avatar_det"] else None)


class Api:
    def __init__(self, engine, app_ctx):
        self.e = engine
        self.ctx = app_ctx

    # ---------------------------------------------------------------- GET
    def get(self, path, qs):
        e = self.e
        if path == "/api/state":
            st = e.status()
            st["port"] = self.ctx["port"]
            st["port_locked"] = self.ctx["port_locked"]
            st["ui"] = e.cfg.get("ui")
            return st
        if path == "/api/live":
            return e.live
        if path == "/api/meta":
            return dict(groups=GROUPS, classes=[dict(id=c, de=DE.get(c, c),
                                                     group=CLASS_GROUP[c]["id"] if c in CLASS_GROUP else "other")
                                                for c in COCO], version=e.version)
        if path == "/api/config":
            cfg = e.cfg.get()
            if cfg["mqtt"].get("password"):
                cfg["mqtt"]["password"] = "********"
            for st in cfg["streams"]:
                st["url"] = mask(st.get("url", ""))
            return cfg
        if path == "/api/log":
            return list(e.logbuf)
        if path == "/api/update":
            return e.updater.public()
        if path == "/api/detections":
            return self.detections(qs)
        m = re.match(r"^/api/detection/(\d+)$", path)
        if m:
            r = e.store.q("SELECT * FROM detections WHERE id=?", (int(m.group(1)),), one=True)
            if not r:
                raise KeyError("Bild nicht gefunden")
            d = det_json(e, r, obj_labels(e, [r["object_id"]]))
            d["track_count"] = e.store.q("SELECT COUNT(*) n FROM detections WHERE track=?", (r["track"],),
                                         one=True)["n"]
            return d
        if path == "/api/objects":
            return self.objects(qs)
        m = re.match(r"^/api/object/(\d+)$", path)
        if m:
            oid = int(m.group(1))
            o = e.store.q("SELECT o.*, c.name cat, c.color ccol, (SELECT GROUP_CONCAT(DISTINCT stream_name) FROM "
                          "detections WHERE object_id=o.id) streams FROM objects o LEFT JOIN categories c ON "
                          "c.id=o.category_id WHERE o.id=?", (oid,), one=True)
            if not o:
                raise KeyError("Objekt nicht gefunden")
            o["streams"] = (o["streams"] or "").replace(",", "\x1f")
            res = obj_json(o)
            rows = e.store.q("SELECT * FROM detections WHERE object_id=? ORDER BY ts DESC LIMIT ?",
                             (oid, int(qs.get("limit", ["300"])[0])))
            labels = obj_labels(e, [oid])
            res["items"] = [det_json(e, r, labels) for r in rows]
            res["samples"] = e.store.q("SELECT kind, neg, COUNT(*) n FROM samples WHERE object_id=? GROUP BY kind, neg",
                                       (oid,))
            return res
        if path == "/api/categories":
            return e.store.q("SELECT c.*, (SELECT COUNT(*) FROM objects WHERE category_id=c.id) n FROM categories c "
                             "ORDER BY sort, name")
        if path == "/api/alarms":
            return e.store.q("SELECT * FROM alarms ORDER BY id")
        if path == "/api/falsepos":
            rows = e.store.q("SELECT id, ts, stream_id, stream_name, cls, x1, y1, x2, y2, crop FROM false_pos "
                             "ORDER BY id DESC LIMIT 500")
            for r in rows:
                r["cls_de"] = DE.get(r["cls"], r["cls"])
                r["crop"] = "/falsepos/" + r["crop"] if r["crop"] else None
            return rows
        if path == "/api/events":
            lim = int(qs.get("limit", ["100"])[0])
            rows = e.store.q("SELECT a.*, d.crop FROM alarm_events a LEFT JOIN detections d ON d.id=a.det_id "
                             "ORDER BY a.ts DESC LIMIT ?", (lim,))
            for r in rows:
                r["crop"] = "/files/" + r["crop"] if r.get("crop") else None
            return rows
        if path == "/api/stats":
            st = dict(detections=e.store.q("SELECT COUNT(*) n FROM detections", one=True)["n"],
                      objects=e.store.q("SELECT COUNT(*) n FROM objects", one=True)["n"],
                      disk_mb=round(e.disk_usage() / 1048576.0, 1))
            st["by_group"] = {}
            for r in e.store.q("SELECT cls, COUNT(*) n FROM detections WHERE sighting=1 AND ts>=? GROUP BY cls",
                               (time.time() - 86400,)):
                g = CLASS_GROUP.get(r["cls"], {"id": "other"})["id"]
                st["by_group"][g] = st["by_group"].get(g, 0) + r["n"]
            return st
        raise KeyError("unbekannt")

    def detections(self, qs):
        e = self.e
        where, args = ["1"], []

        def p(k):
            v = qs.get(k, [""])[0]
            return v.strip()

        if p("stream"):
            where.append("d.stream_id=?")
            args.append(p("stream"))
        if p("group"):
            cl = next((g["classes"] for g in GROUPS if g["id"] == p("group")), [])
            where.append("d.cls IN (%s)" % ",".join("?" * len(cl)) if cl else "0")
            args += cl
        if p("cls"):
            where.append("d.cls=?")
            args.append(p("cls"))
        if p("object"):
            where.append("d.object_id=?")
            args.append(int(p("object")))
        if p("category"):
            where.append("d.object_id IN (SELECT id FROM objects WHERE category_id=?)")
            args.append(int(p("category")))
        if p("named") == "1":
            where.append("d.object_id IN (SELECT id FROM objects WHERE name<>'')")
        if p("named") == "0":
            where.append("(d.object_id IS NULL OR d.object_id IN (SELECT id FROM objects WHERE name=''))")
        if p("exclude_object"):
            where.append("(d.object_id IS NULL OR d.object_id<>?)")
            args.append(int(p("exclude_object")))
        if p("like_object"):
            # nur Erkennungen, die zur Art dieses Objekts passen (Person -> Personen, Auto -> Auto/LKW/Bus)
            o = e.store.q("SELECT cls FROM objects WHERE id=?", (int(p("like_object")),), one=True)
            if o:
                same = [c for c in COCO if track_key(c) == track_key(o["cls"])]
                where.append("d.cls IN (%s)" % ",".join("?" * len(same)))
                args += same
        if p("sightings") == "1":
            where.append("d.sighting=1")
        if p("from"):
            where.append("d.ts>=?")
            args.append(float(p("from")))
        if p("to"):
            where.append("d.ts<?")
            args.append(float(p("to")))
        total = e.store.q("SELECT COUNT(*) n FROM detections d WHERE " + " AND ".join(where), args, one=True)["n"]
        if p("before"):
            where.append("d.id<?")
            args.append(int(p("before")))
        lim = max(1, min(500, int(p("limit") or 120)))
        rows = e.store.q("SELECT d.id, d.ts, d.stream_id, d.stream_name, d.cls, d.score, d.x1, d.y1, d.x2, d.y2, "
                         "d.object_id, d.track, d.sighting, d.face, d.match, d.sim, d.img, d.crop, d.manual "
                         "FROM detections d WHERE " + " AND ".join(where) + " ORDER BY d.id DESC LIMIT ?",
                         args + [lim])
        labels = obj_labels(e, [r["object_id"] for r in rows])
        return dict(items=[det_json(e, r, labels) for r in rows], total=total)

    def objects(self, qs):
        e = self.e

        def p(k):
            return qs.get(k, [""])[0].strip()

        where, args = ["1"], []
        mn = p("min")
        if mn:
            where.append("(o.sightings>=? OR o.name<>'' OR o.category_id IS NOT NULL)")
            args.append(int(mn))
        if p("group"):
            cl = next((g["classes"] for g in GROUPS if g["id"] == p("group")), [])
            where.append("o.cls IN (%s)" % ",".join("?" * len(cl)) if cl else "0")
            args += cl
        if p("category"):
            if p("category") == "none":
                where.append("o.category_id IS NULL")
            else:
                where.append("o.category_id=?")
                args.append(int(p("category")))
        if p("q"):
            where.append("(o.name LIKE ? OR o.notes LIKE ?)")
            args += ["%" + p("q") + "%"] * 2
        if p("named") == "1":
            where.append("o.name<>''")
        if p("cls"):
            where.append("o.cls=?")
            args.append(p("cls"))
        order = {"last": "o.last_seen DESC", "count": "o.sightings DESC, o.last_seen DESC",
                 "name": "CASE WHEN o.name='' THEN 1 ELSE 0 END, o.name COLLATE NOCASE, o.id",
                 "first": "o.first_seen DESC"}.get(p("sort") or "last", "o.last_seen DESC")
        lim = max(1, min(1000, int(p("limit") or 300)))
        rows = e.store.q("SELECT o.*, c.name cat, c.color ccol, (SELECT GROUP_CONCAT(stream_name, char(31)) FROM "
                         "(SELECT DISTINCT stream_name FROM detections WHERE object_id=o.id)) streams FROM objects o "
                         "LEFT JOIN categories c ON c.id=o.category_id WHERE " + " AND ".join(where)
                         + " ORDER BY " + order + " LIMIT ?", args + [lim])
        return [obj_json(o) for o in rows]

    # ---------------------------------------------------------------- POST / DELETE
    def post(self, path, body):
        e = self.e
        if path == "/api/config":
            sec, vals = body.get("section"), body.get("values")
            if sec not in ("detection", "identity", "storage", "mqtt", "ui", "server", "update", "fan"):
                raise ValueError("Bereich unbekannt")
            if sec == "mqtt" and vals.get("password") == "********":
                vals.pop("password")
            if sec == "server":
                p = int(vals.get("port", 8096))
                if not 1 <= p <= 65535:
                    raise ValueError("Port ungültig")
                vals = dict(port=p)
            old = e.cfg.get("detection")
            e.cfg.update(sec, vals)
            if sec == "detection":
                new = e.cfg.get("detection")
                if any(new.get(k) != old.get(k) for k in ("model", "model_size", "use_opencv", "accel")):
                    threading.Thread(target=e.reload_detector, daemon=True).start()
            return dict(ok=True)
        if path == "/api/streams/test":
            url = (body.get("url") or "").strip()
            old = next((x for x in e.cfg.get("streams") if x["id"] == body.get("id")), None)
            if old and ("***" in url or not url):
                url = old.get("url", "")
            from .streamtest import run_test
            res = run_test(url)
            e.log("Stream-Test %s: %s" % (mask(url), " | ".join(
                "%s %s" % (st["name"], {True: "ok", False: "FEHLER", None: "-"}[st["ok"]]) for st in res["steps"])))
            return res
        if path == "/api/streams":
            lst = []
            for s in body.get("streams", []):
                url = (s.get("url") or "").strip()
                sid = re.sub(r"[^a-zA-Z0-9_-]", "", s.get("id") or "") or "s" + uuid.uuid4().hex[:6]
                old = next((x for x in e.cfg.get("streams") if x["id"] == sid), None)
                if url == "********" or (old and "***" in url and url == mask(old.get("url", ""))):
                    url = old["url"] if old else ""
                lst.append(dict(id=sid, name=(s.get("name") or sid).strip()[:60], url=url,
                                enabled=bool(s.get("enabled", True)), detect=bool(s.get("detect", True)),
                                fps=float(s.get("fps") or 2), width=int(s.get("width") or 640),
                                keyframes_only=bool(s.get("keyframes_only")), transport=s.get("transport") if s.get("transport") in ("auto", "tcp", "udp") else "auto",
                                decoder=s.get("decoder") if s.get("decoder") in ("auto", "nvdec", "gst", "ffmpeg") else "auto"))
            e.cfg.update("streams", lst)
            e.sync_streams()
            return dict(ok=True)
        m = re.match(r"^/api/detection/(\d+)/move$", path)
        if m:
            did = int(m.group(1))
            ids = [did]
            if body.get("whole_track"):
                r = e.store.q("SELECT track FROM detections WHERE id=?", (did,), one=True)
                if r and r["track"]:
                    ids = [x["id"] for x in e.store.q("SELECT id FROM detections WHERE track=?", (r["track"],))]
            tgt = e.move_detections(ids, body.get("target"))
            if body.get("name") and tgt:
                e.store.x("UPDATE objects SET name=? WHERE id=? AND name=''", (body["name"].strip()[:80], tgt))
            return dict(ok=True, object_id=tgt, moved=len(ids))
        m = re.match(r"^/api/objects/(\d+)$", path)
        if m:
            oid = int(m.group(1))
            sets, args = [], []
            for k in ("name", "notes"):
                if k in body:
                    sets.append(k + "=?")
                    args.append((body[k] or "").strip()[:500 if k == "notes" else 80])
            if "category_id" in body:
                sets.append("category_id=?")
                args.append(int(body["category_id"]) if body["category_id"] not in (None, "", "none") else None)
            if sets:
                e.store.x("UPDATE objects SET %s WHERE id=?" % ", ".join(sets), args + [oid])
            return dict(ok=True)
        if path == "/api/objects":
            cid = body.get("category_id")
            oid = e.create_object(body.get("cls") or "person", body.get("name") or "",
                                  int(cid) if cid not in (None, "", "none") else None, body.get("notes") or "")
            return dict(ok=True, id=oid)
        if path == "/api/detections/assign":
            ids = [int(i) for i in body.get("ids", [])]
            if not ids:
                raise ValueError("keine Bilder ausgewählt")
            if body.get("whole_track"):
                tracks = [r["track"] for r in e.store.q("SELECT DISTINCT track FROM detections WHERE id IN (%s)"
                                                         % ",".join(map(str, ids))) if r["track"]]
                for t in tracks:
                    ids += [r["id"] for r in e.store.q("SELECT id FROM detections WHERE track=?", (t,))]
                ids = sorted(set(ids))
            target = body.get("target")
            if target in (None, "", "new") and body.get("name"):
                # neue Person/neues Objekt mit Namen anlegen, Art vom ersten Bild
                r = e.store.q("SELECT cls FROM detections WHERE id=?", (ids[0],), one=True)
                target = e.create_object(r["cls"] if r else "person", body["name"],
                                         int(body["category_id"]) if body.get("category_id") else None)
            tgt = e.move_detections(ids, target)
            return dict(ok=True, object_id=tgt, moved=len(ids) - e.last_move_skipped, skipped=e.last_move_skipped)
        if path == "/api/detections/false":
            return dict(ok=True, learned=e.mark_false([int(i) for i in body.get("ids", [])]))
        if path == "/api/detections/delete":
            ids = [int(i) for i in body.get("ids", [])]
            objs = {r["object_id"] for r in e.store.q("SELECT object_id FROM detections WHERE id IN (%s)"
                                                       % ",".join(map(str, ids)))} if ids else set()
            for i in ids:
                e.delete_detection(i, recount=False)
            for oid in objs:
                if oid:
                    e.recount(oid)
                    e.index.reload_obj(oid)
                    e._cleanup_object(oid)
            return dict(ok=True, deleted=len(ids))
        if path == "/api/purge":
            n = e.purge(objects=bool(body.get("objects")), falsepos=bool(body.get("falsepos")),
                        alarms=bool(body.get("alarms")))
            return dict(ok=True, deleted=n)
        m = re.match(r"^/api/detection/(\d+)/false$", path)
        if m:
            did = int(m.group(1))
            ids = [did]
            if body.get("whole_track"):
                r = e.store.q("SELECT track FROM detections WHERE id=?", (did,), one=True)
                if r and r["track"]:
                    ids = [x["id"] for x in e.store.q("SELECT id FROM detections WHERE track=?", (r["track"],))]
            return dict(ok=True, learned=e.mark_false(ids))
        m = re.match(r"^/api/objects/(\d+)/false$", path)
        if m:
            ids = [x["id"] for x in e.store.q("SELECT id FROM detections WHERE object_id=?", (int(m.group(1)),))]
            n = e.mark_false(ids)
            if e.store.q("SELECT id FROM objects WHERE id=?", (int(m.group(1)),), one=True):
                e.delete_object(int(m.group(1)), delete_detections=True)
            return dict(ok=True, learned=n)
        m = re.match(r"^/api/objects/(\d+)/merge$", path)
        if m:
            e.merge_objects(int(m.group(1)), int(body["into"]))
            return dict(ok=True)
        if path == "/api/categories":
            name = (body.get("name") or "").strip()[:60]
            if not name:
                raise ValueError("Name fehlt")
            color = body.get("color") or "#ff9142"
            if body.get("id"):
                e.store.x("UPDATE categories SET name=?, color=? WHERE id=?", (name, color, int(body["id"])))
                return dict(ok=True, id=int(body["id"]))
            cid = e.store.x("INSERT INTO categories(name, color) VALUES(?,?)", (name, color))
            return dict(ok=True, id=cid)
        if path == "/api/alarms":
            f = dict(name=(body.get("name") or "Alarm").strip()[:80], enabled=1 if body.get("enabled", True) else 0,
                     target_type=body.get("target_type") or "class", target=str(body.get("target") or "person"),
                     count_mode=body.get("count_mode") or "per_object", min_count=int(body.get("min_count") or 3),
                     window_min=float(body.get("window_min") or 30), min_streams=int(body.get("min_streams") or 1),
                     cooldown_min=float(body.get("cooldown_min") or 0), only_named=int(body.get("only_named") or 0),
                     mqtt=1 if body.get("mqtt", True) else 0, webhook=(body.get("webhook") or "").strip(),
                     sound=1 if body.get("sound", True) else 0)
            if f["target_type"] not in ("object", "category", "class", "any"):
                raise ValueError("Ziel unbekannt")
            if body.get("id"):
                e.store.x("UPDATE alarms SET %s WHERE id=?" % ", ".join(k + "=?" for k in f),
                          list(f.values()) + [int(body["id"])])
                e.cooldown = {k: v for k, v in e.cooldown.items() if k[0] != int(body["id"])}
                return dict(ok=True, id=int(body["id"]))
            aid = e.store.x("INSERT INTO alarms(%s) VALUES(%s)" % (",".join(f), ",".join("?" * len(f))),
                            list(f.values()))
            return dict(ok=True, id=aid)
        m = re.match(r"^/api/alarms/(\d+)/test$", path)
        if m:
            return dict(ok=e.test_alarm(int(m.group(1))))
        if path == "/api/events/ack":
            if body.get("all"):
                e.store.x("UPDATE alarm_events SET ack=1 WHERE ack=0")
            else:
                ids = [int(i) for i in body.get("ids", [])]
                if ids:
                    e.store.x("UPDATE alarm_events SET ack=1 WHERE id IN (%s)" % ",".join(map(str, ids)))
            return dict(ok=True)
        if path == "/api/events/clear":
            e.store.x("DELETE FROM alarm_events")
            return dict(ok=True)
        if path == "/api/models/download":
            e.models.ensure(on_done=e.load_models)
            return dict(ok=True)
        if path == "/api/engines/retry":
            e.models.retry_engines()
            threading.Thread(target=e.load_models, daemon=True).start()
            return dict(ok=True)
        if path == "/api/cleanup":
            threading.Thread(target=e.cleanup, daemon=True).start()
            return dict(ok=True)
        if path == "/api/mqtt/test":
            cfg = e.cfg.get("mqtt")
            ok = e.mqtt.publish(cfg, "%s/test" % (cfg.get("prefix") or "sentinel").strip("/"),
                                json.dumps(dict(text="Sentinel MK1 Test", time=time.strftime("%H:%M:%S"))))
            return dict(ok=ok, error=e.mqtt.last_error)
        if path == "/api/update/check":
            return e.updater.check()
        if path == "/api/update/install":
            if not e.updater.start_update(force=bool(body.get("force"))):
                raise ValueError("Update läuft bereits")
            return dict(ok=True)
        if path == "/api/restart":
            threading.Thread(target=self.ctx["restart"], daemon=True).start()
            return dict(ok=True)
        raise KeyError("unbekannt")

    def delete(self, path, qs):
        e = self.e
        m = re.match(r"^/api/detection/(\d+)$", path)
        if m:
            e.delete_detection(int(m.group(1)))
            return dict(ok=True)
        m = re.match(r"^/api/objects/(\d+)$", path)
        if m:
            e.delete_object(int(m.group(1)), delete_detections=qs.get("images", ["1"])[0] == "1")
            return dict(ok=True)
        m = re.match(r"^/api/categories/(\d+)$", path)
        if m:
            cid = int(m.group(1))
            e.store.xmany([("UPDATE objects SET category_id=NULL WHERE category_id=?", (cid,)),
                           ("DELETE FROM categories WHERE id=?", (cid,))])
            return dict(ok=True)
        m = re.match(r"^/api/alarms/(\d+)$", path)
        if m:
            e.store.x("DELETE FROM alarms WHERE id=?", (int(m.group(1)),))
            return dict(ok=True)
        m = re.match(r"^/api/falsepos/(\d+|alle)$", path)
        if m:
            fdir = os.path.join(e.data_dir, "falsepos")
            rows = e.store.q("SELECT id, crop FROM false_pos" + ("" if m.group(1) == "alle" else " WHERE id=%d" % int(m.group(1))))
            for r in rows:
                if r["crop"]:
                    try:
                        os.remove(os.path.join(fdir, r["crop"]))
                    except OSError:
                        pass
                e.store.x("DELETE FROM false_pos WHERE id=?", (r["id"],))
            e.fp_cache = None
            return dict(ok=True, deleted=len(rows))
        raise KeyError("unbekannt")


def mask(url):
    return re.sub(r"(://[^:/@]+:)[^@]*@", r"\1***@", url or "")


def make_handler(engine, api):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "SentinelMK1"

        def log_message(self, fmt, *args):
            pass

        def _json(self, code, obj):
            b = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b)

        def _file(self, path, cache=False):
            if not os.path.isfile(path):
                return self._json(404, dict(error="nicht gefunden"))
            ct = mimetypes.guess_type(path)[0] or "application/octet-stream"
            if ct.startswith("text/") or ct.endswith("javascript"):
                ct += "; charset=utf-8"
            data = open(path, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=86400" if cache else "no-cache")
            self.end_headers()
            self.wfile.write(data)

        def _bytes(self, data, ct, idx=None):
            self.send_response(200)
            self.send_header("Content-Type", ct)
            if idx is not None:
                self.send_header("X-Frame", str(idx))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}")

        def do_GET(self):
            u = urlparse(self.path)
            path = unquote(u.path)
            qs = parse_qs(u.query)
            try:
                if path in ("/", "/index.html"):
                    return self._file(os.path.join(STATIC, "index.html"))
                if path.startswith("/static/"):
                    p = os.path.normpath(os.path.join(STATIC, path[8:]))
                    if not p.startswith(STATIC):
                        return self._json(403, dict(error="verboten"))
                    return self._file(p)
                if path.startswith("/files/"):
                    p = os.path.normpath(os.path.join(engine.snap_dir, path[7:]))
                    if not p.startswith(engine.snap_dir):
                        return self._json(403, dict(error="verboten"))
                    return self._file(p, cache=True)
                m = re.match(r"^/falsepos/(fp_\d+\.png)$", path)
                if m:
                    return self._file(os.path.join(engine.data_dir, "falsepos", m.group(1)), cache=True)
                m = re.match(r"^/avatar/(\d+)\.png$", path)
                if m:
                    return self._file(os.path.join(engine.avatar_dir, "%s.png" % m.group(1)), cache=True)
                m = re.match(r"^/api/stream/([\w-]+)\.mjpg$", path)
                if m:
                    return self._mjpeg(m.group(1))
                m = re.match(r"^/api/stream/([\w-]+)(/analyzed)?\.jpg$", path)
                if m:
                    idx = None
                    if m.group(2):
                        a = engine.analyzed.get(m.group(1))
                        j = a[1] if a else None
                    else:
                        w = engine.workers.get(m.group(1))
                        jj = None
                        if w:
                            after = qs.get("after", [""])[0]
                            jj = w.wait_jpeg(int(after), timeout=4) if after.lstrip("-").isdigit() else w.latest_jpeg()
                        j = jj[2] if jj else None
                        idx = jj[0] if jj else None
                    if not j:
                        return self._json(404, dict(error="kein Bild"))
                    return self._bytes(j, "image/jpeg", idx)
                if path.startswith("/api/"):
                    return self._json(200, api.get(path, qs))
                return self._json(404, dict(error="nicht gefunden"))
            except KeyError as ex:
                return self._json(404, dict(error=str(ex).strip("'")))
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as ex:
                engine.log("HTTP GET %s: %s" % (path, ex))
                try:
                    return self._json(500, dict(error=str(ex)))
                except Exception:
                    pass

        def _mjpeg(self, sid):
            w = engine.workers.get(sid)
            if not w:
                return self._json(404, dict(error="Stream unbekannt"))
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=sentinelframe")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            last = None
            t_end = time.time() + 3600
            try:
                while time.time() < t_end:
                    j = w.wait_jpeg(last, timeout=5)
                    if engine.workers.get(sid) is not w:
                        break
                    if not j or j[0] == last:
                        continue
                    last = j[0]
                    data = j[2]
                    self.wfile.write(b"--sentinelframe\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n"
                                     % len(data) + data + b"\r\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

        def _write(self, fn):
            u = urlparse(self.path)
            path = unquote(u.path)
            try:
                return self._json(200, fn(path, u))
            except KeyError as ex:
                return self._json(404, dict(error=str(ex).strip("'")))
            except ValueError as ex:
                return self._json(400, dict(error=str(ex)))
            except Exception as ex:
                engine.log("HTTP %s %s: %s" % (self.command, path, ex))
                return self._json(500, dict(error=str(ex)))

        def do_POST(self):
            return self._write(lambda p, u: api.post(p, self._body()))

        def do_DELETE(self):
            return self._write(lambda p, u: api.delete(p, parse_qs(u.query)))

    return H


class Server(socketserver.ThreadingMixIn, HTTPServer):
    """Mehrere Anfragen gleichzeitig (ThreadingHTTPServer gibt es erst ab Python 3.7 - Jetson hat 3.6)."""
    daemon_threads = True
    allow_reuse_address = True
