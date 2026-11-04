"""Contact form: messages are saved for the owner (read in /admin) and mailed to ADMIN_EMAIL when e-mail works."""
from . import config as cfg
from .core import go, read_form, render, route
from .mail import send_mail_bg
from .security import client_ip, limiter
from .utils import clean, valid_email
from starlette.exceptions import HTTPException


@route
async def contact(request, conn):
    if request.method == "GET":
        return render(request, "contact.html", form={})
    form = await read_form(request)
    data = {"name": clean(str(form.get("name", "")), 60), "email": clean(str(form.get("email", "")), 254),
            "message": clean(str(form.get("message", "")), 2000)}
    if form.get("website"):   # honeypot: only bots fill it. Pretend it worked.
        return go(request, "/contact", msg="Thank you! We will get back to you.")
    if not limiter.allow(f"contact:{client_ip(request)}", cfg.CONTACT_PER_HOUR, 3600):
        raise HTTPException(429, "Too many messages. Please try again later.")
    if not data["name"]:
        error = "Enter your name."
    elif not valid_email(data["email"]):
        error = "Enter a valid email address so we can reply."
    elif len(data["message"]) < 10:
        error = "Please write a few words about what you need."
    else:
        error = None
    if error:
        return render(request, "contact.html", 400, form=data, error=error)
    with conn:
        conn.execute("INSERT INTO contact_messages (name, email, message) VALUES (?, ?, ?)",
                     (data["name"], data["email"], data["message"]))
    if cfg.ADMIN_EMAIL:
        send_mail_bg(cfg.ADMIN_EMAIL, f"New message from {data['name']}",
                     f"From: {data['name']} <{data['email']}>\n\n{data['message']}")
    return go(request, "/contact", msg="Thank you! We will get back to you.")

