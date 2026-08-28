"""Signup, login, and account Flask routes."""

from collections.abc import Callable

from flask import Blueprint, jsonify, render_template, request


def create_blueprint(
    *,
    create_magic_link_token: Callable[[str], str],
    deliver_magic_link: Callable[[str, str], bool],
    expose_magic_link_token: Callable[[], bool],
    consume_magic_link: Callable[[str], dict | None],
    current_session: Callable[[], dict | None],
    auth_db: Callable,
) -> Blueprint:
    blueprint = Blueprint("account", __name__)

    @blueprint.route("/signup", methods=["GET", "POST"])
    def signup_page():
        if request.method == "GET":
            return render_template("signup.html")

        data = request.json or {}
        email = (data.get("email") or "").strip().lower()
        if not email or "@" not in email or "." not in email.split("@")[-1]:
            return jsonify({"error": "Invalid email"}), 400
        token = create_magic_link_token(email)
        if not deliver_magic_link(email, token):
            if expose_magic_link_token():
                return jsonify({"token": token, "email": email, "delivery": "development"})
            return jsonify({"error": "Email delivery is not configured"}), 503
        return jsonify({"email": email, "delivery": "email"}), 202

    @blueprint.route("/login", methods=["GET", "POST"])
    def login_page():
        if request.method == "GET":
            return render_template("login.html")

        data = request.json or {}
        token = (data.get("token") or "").strip()
        if not token:
            return jsonify({"error": "Token required"}), 400
        session = consume_magic_link(token)
        if not session:
            return jsonify({"error": "Invalid or expired token"}), 400
        return jsonify(
            {
                "session_token": session["id"],
                "email": session["email"],
                "credits_remaining": session["credits_remaining"],
                "expires_at": session["expires_at"],
            }
        )

    @blueprint.get("/api/me")
    def api_me():
        session = current_session()
        if not session:
            return jsonify({"error": "Not signed in"}), 401
        with auth_db() as database:
            user = database.execute(
                "SELECT * FROM users WHERE id = ?", (session["user_id"],)
            ).fetchone()
        if not user:
            return jsonify({"error": "User not found"}), 401
        return jsonify(
            {
                "email": user["email"],
                "credits_remaining": user["credits_remaining"] or 0,
                "credits_used_today": user["credits_used_today"] or 0,
                "created_at": user["created_at"],
                "subscription_tier": user["subscription_tier"],
                "subscription_status": user["subscription_status"],
                "subscription_renews_at": user["subscription_renews_at"],
            }
        )

    return blueprint
