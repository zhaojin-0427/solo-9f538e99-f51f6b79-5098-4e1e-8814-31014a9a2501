# -*- coding: utf-8 -*-
"""Flask 应用工厂与路由。"""

from __future__ import annotations

import csv
import io
import os

from flask import (
    Flask,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from . import service
from .db import init_db


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("SCHED_SECRET_KEY", "dev-secret-change-me")
    init_db()

    @app.get("/")
    def index():
        itineraries = service.get_overview()
        return render_template("index.html", itineraries=itineraries)

    @app.post("/upload")
    def upload():
        itinerary_no = (request.form.get("itinerary_no") or "").strip()
        rev_text = (request.form.get("revision_no") or "").strip()
        try:
            revision_no = int(rev_text)
            if revision_no <= 0:
                raise ValueError
        except ValueError:
            flash("修订号必须为正整数", "error")
            return redirect(url_for("index"))

        file = request.files.get("schedule_csv")
        if file is None or file.filename == "":
            flash("请选择要上传的 CSV 文件", "error")
            return redirect(url_for("index"))
        raw = file.read()
        if not raw:
            flash("上传的 CSV 文件为空", "error")
            return redirect(url_for("index"))

        result = service.submit(itinerary_no, revision_no, raw)
        outcome = result.get("outcome")
        if result.get("ok"):
            flash(result.get("message", "处理完成"), "success")
        else:
            # 结构性错误附带具体原因（如缺列、航段数不符），直接在 flash 中可见
            if outcome == "structural" and result.get("reasons"):
                prefix = result.get("message", "提交被拒绝")
                flash(prefix + "：" + "；".join(result["reasons"]), "error")
            else:
                flash(result.get("message", "提交被拒绝"), "error")
        # 结构性错误发生在行程落库之前；行程不存在时回首页，否则进详情页
        if outcome == "structural" and service.get_detail(itinerary_no) is None:
            return redirect(url_for("index"))
        return redirect(url_for("itinerary_detail", itinerary_no=itinerary_no,
                                outcome=outcome, rev=revision_no))

    @app.get("/itinerary/<path:itinerary_no>")
    def itinerary_detail(itinerary_no: str):
        # 只读一次：整页（当前修订号 + 修订历史 + UTC 航段）取自同一 WAL 快照，
        # 并发发布不会让页面上出现新旧版本混排。
        outcome = request.args.get("outcome")
        rev = request.args.get("rev", type=int)
        detail = service.get_detail(
            itinerary_no, latest_rev=rev if outcome and rev is not None else None
        )
        if detail is None:
            abort(404, description=f"行程 {itinerary_no} 不存在")
        latest = None
        if outcome and rev is not None:
            latest = {"outcome": outcome, "revision_no": rev,
                      "submission": detail.get("latest_submission")}
        return render_template("detail.html", d=detail, latest=latest)

    @app.get("/download/<path:itinerary_no>")
    def download(itinerary_no: str):
        revision_no = request.args.get("revision", type=int)
        found = service.get_published_timeline(itinerary_no, revision_no)
        if found is None:
            abort(404, description="没有可下载的完整已发布版本（该修订不存在或未发布）")
        segments, rev_no = found
        lines = service.export_published_csv(segments)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerows(lines)
        filename = f"{itinerary_no}_R{rev_no}_utc.csv"
        return Response(
            buf.getvalue(),
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    @app.get("/samples/<name>")
    def sample(name: str):
        samples_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "samples")
        safe = os.path.basename(name)
        path = os.path.join(samples_dir, safe)
        if not safe.endswith(".csv") or not os.path.isfile(path):
            abort(404)
        with open(path, "rb") as fh:
            data = fh.read()
        return Response(
            data,
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename={safe}"},
        )

    @app.errorhandler(404)
    def not_found(err):
        return render_template("error.html", message=getattr(err, "description", "页面不存在")), 404

    return app
