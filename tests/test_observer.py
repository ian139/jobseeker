from jobs_assistant.contracts import FieldKind
from jobs_assistant.observer import observe_static_html


def test_observer_extracts_fields_buttons_errors_and_blockers():
    html = """
    <html><head><title>Apply</title></head><body>
      <form>
        <label for="name">Full name</label><input id="name" name="name" required>
        <label>Remote OK <input type="checkbox" name="remote" required checked></label>
        <label for="role">Role</label><select id="role" required><option>Engineer</option></select>
        <input type="file" name="resume" required>
        <div role="alert">Name required</div>
        <button type="button">Continue</button><button type="submit">Submit application</button>
      </form>
      <p>Captcha appears here</p>
    </body></html>
    """
    snapshot = observe_static_html(html, url="https://a.test")
    assert snapshot.title == "Apply"
    assert any(field.label == "Full name" and field.kind == FieldKind.TEXT for field in snapshot.fields)
    assert any(field.kind == FieldKind.FILE for field in snapshot.fields)
    assert any(button.final_submit_candidate for button in snapshot.buttons)
    assert snapshot.errors == ("Name required",)
    assert "captcha" in snapshot.blockers


def test_observer_treats_button_without_type_as_submit_candidate():
    snapshot = observe_static_html("<form><button>Submit application</button></form>")
    assert snapshot.buttons[0].type == "submit"
    assert snapshot.buttons[0].final_submit_candidate is True
