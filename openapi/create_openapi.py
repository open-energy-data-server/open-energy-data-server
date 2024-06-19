import requests
from flask import Flask, render_template, redirect
from functools import lru_cache
app = Flask(__name__)

@app.route('/')
def root():
    return redirect("/docs")

@app.route('/docs/')
@lru_cache(10)
def swagger_list():
    resp = requests.get("https://monitor.nowum.fh-aachen.de/oeds/", headers={"Accept-Profile": "dummy"})
    schemas_str = resp.content.decode("utf-8").split(": ")[-1].split('"')[0]
    schemas = schemas_str.split(", ")

    return render_template("root.html", schemas=schemas)

@app.route('/docs/<schema>')
def swagger_ui(schema):
    return render_template('swagger_ui.html', schema=schema)


@app.route('/spec/<schema>')
@lru_cache(10)
def get_spec(schema):
    response = requests.get("https://monitor.nowum.fh-aachen.de/oeds/", headers={"Accept-Profile": schema})
    js = response.json()

    header = {
        "securityDefinitions": {
        "Accept-Profile": {
            "type": "apiKey",
            "in": "header",
            "name": "Accept-Profile"
           }
        }
    }
    js.update(header)
    return js


if __name__ == "__main__":
    app.run()