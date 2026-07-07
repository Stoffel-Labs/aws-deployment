import importlib.util
import os


def _load_app_module():
    # app.py guards its stack-building/synth side effects behind
    # `if __name__ == "__main__":`, so importing it here is cheap — it does
    # not invoke cdk synth or trigger any Docker asset builds.
    path = os.path.join(os.path.dirname(__file__), "..", "..", "app.py")
    spec = importlib.util.spec_from_file_location("cross_region_app", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_region_assignment_is_valid():
    app = _load_app_module()

    assert len(app.PARTY_REGIONS) == app.N_PARTIES
    assert len(set(app.PARTY_REGIONS)) == len(app.PARTY_REGIONS), "duplicate party region"
    assert app.COORD_REGION not in app.PARTY_REGIONS, "coordinator must not share a party's region"
