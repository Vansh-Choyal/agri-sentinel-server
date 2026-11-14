from flask import Blueprint, render_template, session

from .auth import csrf_token, operator_required
from .services.chat import LANGUAGES, NATIVE_NAMES
from .models import METRICS

views = Blueprint("views", __name__)


@views.get("/")
@views.get("/chatbot")
@operator_required
def dashboard():
    return render_template("dashboard.html", csrf_token=csrf_token(), operator=session["operator"], metrics=METRICS, chat_languages=LANGUAGES, chat_native_names=NATIVE_NAMES)
