"""Odd numbers from the TV (NaN, Infinity, text, lists, huge values): json.loads lets them all through, and none of
them may end the cast, poison the position or break the player's panel."""

import io
import json
import math
import shutil
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from castmymovie import cast, castlink, engine, nativecast, telemetry, ui  # noqa: E402
from castmymovie.castlink import MEDIA_NS, num  # noqa: E402
from test_castlink import FakeTV, reader_for  # noqa: E402
from test_nativecast import Base, QuickCast  # noqa: E402

NAN, INF = float("nan"), float("inf")
HUGE_INT = 10 ** 400
# what the TV can put where a number belongs; the JSON texts are what actually arrives on the wire
ODD = [("NaN", json.loads("NaN")), ("Infinity", json.loads("Infinity")), ("-Infinity", json.loads("-Infinity")),
       ("1e999", json.loads("1e999")), ("text", "abc"), ("numeric text", "12"), ("list", [1]), ("object", {"a": 1}),
       ("true", True), ("null", None), ("huge integer", HUGE_INT), ("huge float", 1e300)]
CURVE = [t * 1000 for t in range(1000)]


def sane(x, top):
    return isinstance(x, float) and math.isfinite(x) and 0.0 <= x <= top


def make_wizard():
    out = io.StringIO()
    console = ui.Console(file=out, theme=ui.THEME, width=130, force_terminal=False)
    return ui.Wizard(console=console, prompt=ui.Prompter(), keys=None), out


class Num(unittest.TestCase):
    def test_numbers_pass_as_floats(self):
        for value in (0, 5, 5.5, -0.0):
            self.assertEqual(num(value, "d"), float(value))
        self.assertIsInstance(num(5), float)

    def test_anything_else_is_the_default(self):
        for label, value in ODD:
            if label in ("huge float", "numeric text"):
                continue
            with self.subTest(label):
                self.assertEqual(num(value, "kept"), "kept")
        self.assertIsNone(num(NAN))

    def test_out_of_range_is_clamped(self):
        self.assertEqual(num(-5.0), 0.0)
        self.assertEqual(num(1e300), 1e6)
        self.assertEqual(num(50, lo=1, hi=10), 10.0)
        self.assertEqual(num(0.2, lo=1, hi=10), 1.0)


class Apply(unittest.TestCase):
    """The status handler, driven directly: no sockets, no thread."""

    def session(self, duration=600.0):
        s = nativecast.NativeCast(cast.Receiver("TV", "127.0.0.1", port=1), "movie.cast.mp4", 100.0, tls=False,
                                  duration=duration)
        s._settled = True
        return s

    def status(self, s, **entry):
        s._on_media_status({"type": "MEDIA_STATUS", "requestId": 0,
                            "status": [dict({"mediaSessionId": 1, "playerState": "PLAYING"}, **entry)]})

    def test_odd_positions_are_ignored_and_the_previous_one_kept(self):
        for label, value in ODD:
            with self.subTest(label):
                s = self.session()
                self.status(s, currentTime=50.0)
                self.status(s, currentTime=value)
                pos = s.state().position
                self.assertTrue(sane(pos, 600.0), pos)
                if label != "huge float":                     # that one is clamped: a position past the end
                    self.assertAlmostEqual(pos, 50.0, delta=5)
                self.assertTrue(math.isfinite(s.tv_status().position))
                self.assertTrue(s.state().alive)

    def test_a_seek_after_an_odd_position_stays_a_number(self):
        s = self.session()
        self.status(s, currentTime=NAN)
        s.seek_by(10)
        self.assertTrue(sane(s.state().position, 600.0), s.state().position)

    def test_odd_rates_are_ignored_and_the_previous_one_kept(self):
        for label, value in ODD:
            with self.subTest(label):
                s = self.session()
                self.status(s, currentTime=50.0, playbackRate=2)
                self.status(s, currentTime=60.0, playbackRate=value)
                rate = s._pb.rate
                self.assertTrue(math.isfinite(rate) and 0 < rate <= 8.0, rate)
                self.assertTrue(math.isfinite(s._pb.position_at(time.monotonic() + 10)))
                self.assertEqual(rate, 8.0 if label == "huge float" else 2.0)

    def test_the_rate_is_clamped_and_zero_means_normal(self):
        s = self.session()
        self.status(s, currentTime=1.0, playbackRate=1e300)
        self.assertEqual(s._pb.rate, 8.0)
        self.status(s, currentTime=1.0, playbackRate=0)
        self.assertEqual(s._pb.rate, 1.0)
        self.status(s, currentTime=1.0, playbackRate=-3)
        self.assertEqual(s._pb.rate, 1.0)

    def test_odd_durations_are_not_adopted(self):
        for label, value in ODD:
            if label == "huge float":
                continue
            with self.subTest(label):
                s = self.session(duration=0.0)
                self.status(s, currentTime=5.0, media={"duration": value})
                self.assertEqual(s.duration, 0.0)
                self.assertTrue(sane(s.state().position, 1e6))
                self.status(s, currentTime=5.0, media={"duration": 600})
                self.assertEqual(s.duration, 600.0)

    def test_a_huge_duration_is_clamped(self):
        s = self.session(duration=0.0)
        self.status(s, currentTime=5.0, media={"duration": 1e300})
        self.assertEqual(s.duration, 1e6)

    def test_a_known_duration_is_not_replaced(self):
        s = self.session(duration=600.0)
        self.status(s, currentTime=5.0, media={"duration": 90})
        self.assertEqual(s.duration, 600.0)

    def test_odd_request_ids_do_not_end_the_cast(self):
        for label, value in ODD:
            with self.subTest(label):
                s = self.session()
                s._on_media_status({"type": "MEDIA_STATUS", "requestId": value,
                                    "status": [{"mediaSessionId": 1, "playerState": "PLAYING", "currentTime": 5.0}]})
                self.assertTrue(sane(s.state().position, 600.0))


class Formatting(unittest.TestCase):
    def test_fmt_dur_survives_non_finite_input(self):
        for value in (NAN, INF, -INF):
            self.assertEqual(engine.fmt_dur(value), "00:00")
        self.assertEqual(engine.fmt_dur(3725), "1:02:05")

    def test_the_estimator_ignores_a_non_finite_position_and_is_not_poisoned(self):
        e = telemetry.BufferEstimator(CURVE, k=1.0)
        self.assertAlmostEqual(e.update_exact(10.0, 40_000), 30.0)
        for value in (NAN, INF, -INF):
            self.assertIsNone(e.update_exact(value, 41_000))
        self.assertAlmostEqual(e.update_exact(11.0, 41_000), 30.0, delta=1)
        self.assertTrue(math.isfinite(e.value))
        e = telemetry.BufferEstimator(CURVE, k=1.0)
        e.begin(0.0)
        self.assertIsNone(e.update(NAN, 40_000))
        self.assertAlmostEqual(e.update(10.0, 40_000), 30.0)

    def test_the_buffer_row_is_left_out_for_a_non_finite_buffer(self):
        w, _ = make_wizard()
        for value in (NAN, INF, -INF):
            self.assertIsNone(w.buffer_row(telemetry.Snapshot(buffer_s=value)))
        self.assertIsNotNone(w.buffer_row(telemetry.Snapshot(buffer_s=31.0)))

    def test_the_panel_renders_with_a_non_finite_snapshot_and_state(self):
        w, out = make_wizard()
        rec = cast.Receiver("TV", "127.0.0.1")
        for value in (NAN, INF):
            snap = telemetry.Snapshot(tv_state="PLAYING", tv_position=value, buffer_s=value, delivered_mbps=1.0)
            st = cast.PlayState(value, 600.0, False, False, True)
            w.console.print(w.render_cast(st, rec, Path("movie.mp4"), True, snap, telemetry_on=True))
        self.assertIn("Casting to TV", out.getvalue())


class StatusReaderOdd(unittest.TestCase):
    """The reader VLC casts use passes the TV's numbers on to the telemetry."""

    def test_odd_numbers_become_none(self):
        class OddTV(FakeTV):
            odd = {}

            def _reply(self, conn, src, ns, payload):
                if payload.get("type") == "MEDIA_STATUS":
                    payload["status"][0].update(self.odd)
                super()._reply(conn, src, ns, payload)

        tv = OddTV()
        self.addCleanup(tv.close)
        r = reader_for(tv)
        self.addCleanup(r.close)
        self.assertTrue(r.open(timeout=3))
        for label, value in ODD:
            with self.subTest(label):
                tv.odd = {"currentTime": value, "media": {"duration": value}}
                st = r.poll()
                self.assertEqual(st.state, "PLAYING")
                for got in (st.position, st.duration):
                    self.assertTrue(got is None or sane(got, 1e6), got)
        tv.odd = {}
        self.assertEqual(r.poll().duration, 600.0)


class OnTheWire(Base):
    """A real session against the fake TV, which sends the odd numbers as JSON text."""

    def setUp(self):
        super().setUp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def feed(self, fake, s, odd, marker):
        """Make the TV answer with `odd`, and return once the cast has handled at least one such answer."""
        fake.odd = odd
        self.wait(lambda: marker in self.log(s), what=f"{marker} to arrive")
        seen = len(fake.kinds(MEDIA_NS))
        self.wait(lambda: len(fake.kinds(MEDIA_NS)) > seen, what="the next poll")

    def panel(self, s):
        w = make_wizard()[0]
        tele = ui.default_telemetry(s, s.receiver, CURVE)
        st = s.state()
        tele.position = st.position
        snap = tele.step()
        w.console.print(w.render_cast(st, s.receiver, Path("movie.mp4"), True, snap, telemetry_on=True))
        return snap

    def check_alive_and_sane(self, fake, s, clamped=False):
        st = s.state()
        self.assertTrue(st.alive, self.log(s))
        self.assertNotIn("BUG", self.log(s))
        self.assertTrue(sane(st.position, 600.0), st.position)
        if not clamped:
            self.assertAlmostEqual(st.position, fake.position(), delta=10)
        snap = self.panel(s)
        self.assertTrue(snap.buffer_s is None or math.isfinite(snap.buffer_s), snap)

    def test_odd_positions_and_rates_leave_the_cast_playing(self):
        fake, s = self.started(start=100.0)
        self.wait(lambda: s.state().position > 100.0, what="the movie to play")
        cases = [("currentTime", NAN, '"currentTime":NaN'), ("currentTime", INF, '"currentTime":Infinity'),
                 ("currentTime", "abc", '"currentTime":"abc"'), ("currentTime", [1], '"currentTime":[1]'),
                 ("currentTime", 1e300, '"currentTime":1e+300'), ("playbackRate", NAN, '"playbackRate":NaN'),
                 ("playbackRate", INF, '"playbackRate":Infinity'), ("playbackRate", "abc", '"playbackRate":"abc"'),
                 ("playbackRate", [1], '"playbackRate":[1]'), ("playbackRate", 1e300, '"playbackRate":1e+300')]
        for field, value, marker in cases:
            with self.subTest(marker):
                self.feed(fake, s, {field: value}, marker)
                self.check_alive_and_sane(fake, s, clamped=(field, value) == ("currentTime", 1e300))
                fake.odd = {}
        self.wait(lambda: abs(s.state().position - fake.position()) < 3, what="the truth to be read again")

    def test_odd_durations_are_not_adopted(self):
        fake = self.fake()
        s = QuickCast(cast.Receiver("Fake TV", "127.0.0.1", port=fake.port), self.media, 100.0, tls=False, duration=0.0)
        self.sessions.append(s)
        s.start(timeout=10)
        media = fake.session["media"]
        for value, marker in ((NAN, '"duration":NaN'), (INF, '"duration":Infinity'), ("abc", '"duration":"abc"'),
                              ([1], '"duration":[1]')):
            with self.subTest(marker):
                self.feed(fake, s, {"media": dict(media, duration=value)}, marker)
                self.assertTrue(s.state().alive, self.log(s))
                self.assertEqual(s.duration, 0.0)
                self.assertTrue(sane(s.state().position, 1e6))
        self.feed(fake, s, {"media": dict(media, duration=600.0)}, '"duration":600.0')
        self.wait(lambda: s.duration == 600.0, what="a good duration to be adopted")
        self.assertTrue(s.state().alive)


if __name__ == "__main__":
    unittest.main()
