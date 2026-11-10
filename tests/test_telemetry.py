import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from castmymovie import linkstats, telemetry  # noqa: E402
from castmymovie.castlink import TvStatus  # noqa: E402

MB = 1_000_000
CURVE = [t * MB for t in range(1000)]          # a movie at a steady 1 MB per second

class ExactEstimator(unittest.TestCase):
    """Native cast: the server knows how far into the file the TV has read, so no byte counting is needed."""

    def est(self):
        return telemetry.BufferEstimator(CURVE, k=telemetry.NATIVE_K)

    def test_the_read_offset_maps_straight_to_media_time(self):
        self.assertAlmostEqual(self.est().update_exact(10.0, 40 * MB), 30.0)     # read up to 40 s, showing 10 s

    def test_no_drift_from_earlier_bytes(self):
        e = self.est()
        e.update_exact(10.0, 40 * MB)
        for t in range(11, 41):                                                  # half a minute of playing
            v = e.update_exact(float(t), (t + 30) * MB)                          # the TV keeps 30 s ahead
        self.assertAlmostEqual(v, 30.0, places=3)

    def test_a_seek_back_restarts_the_download_and_reads_refilling(self):
        e = self.est()
        e.update_exact(100.0, 130 * MB)
        self.assertEqual(e.update_exact(50.0, 50 * MB), 0.0)                     # new download from the seek point
        self.assertTrue(e.reanchored)
        self.assertAlmostEqual(e.update_exact(50.5, 60 * MB), 9.5 * 0.35, places=3)   # smoothing picks up again

    def test_a_stall_is_an_empty_buffer(self):
        e = self.est()
        e.update_exact(10.0, 40 * MB)
        self.assertEqual(e.update_exact(10.0, 40 * MB, stalled=True), 0.0)

    def test_clamped(self):
        self.assertEqual(self.est().update_exact(1.0, 900 * MB), telemetry.MAX_BUFFER_S)
        self.assertEqual(self.est().update_exact(50.0, 1 * MB), 0.0)

    def test_tiny_curve_gives_no_estimate(self):
        self.assertIsNone(telemetry.BufferEstimator([0, 5]).update_exact(1.0, 10))


class Verdicts(unittest.TestCase):
    def test_table(self):
        S = telemetry.Snapshot
        self.assertIsNone(S().verdict)
        self.assertEqual(S(tv_state="PLAYING", buffer_s=30).verdict, "good")
        self.assertEqual(S(buffer_s=10).verdict, "fair")
        self.assertEqual(S(buffer_s=3).verdict, "poor")
        self.assertEqual(S(tv_state="BUFFERING", buffer_s=30).verdict, "poor")
        self.assertEqual(S(buffer_s=30, retx_pct=3).verdict, "fair")
        self.assertEqual(S(buffer_s=30, retx_pct=8).verdict, "poor")
        self.assertEqual(S(buffer_s=30, rtt_ms=400).verdict, "fair")

    def test_a_low_buffer_right_after_a_seek_is_refilling_not_poor(self):
        S = telemetry.Snapshot
        self.assertEqual(S(tv_state="PLAYING", buffer_s=1, refilling=True).verdict, "refilling")
        self.assertEqual(S(tv_state="BUFFERING", buffer_s=1, refilling=True).verdict, "poor")     # a real stall still counts
        self.assertEqual(S(buffer_s=1, refilling=True, retx_pct=9).verdict, "poor")              # so does a lossy link


class Monitor(unittest.TestCase):
    def feed(self, mon, seconds, rate_bytes, retx_rate=0, start=0.0, bytes0=0, retx0=0):
        for i in range(seconds + 1):
            mon.add(linkstats.LinkSample(start + i, bytes0 + rate_bytes * i, retx0 + retx_rate * i, 20.0))

    def test_rate_and_retransmits(self):
        m = linkstats.LinkMonitor()
        self.feed(m, 30, 250_000, retx_rate=2_500)
        h = m.health()
        self.assertAlmostEqual(h.delivered_mbps, 2.0, delta=0.05)
        self.assertAlmostEqual(h.retx_pct, 1.0, delta=0.05)
        self.assertEqual(h.rtt_ms, 20.0)

    def test_not_enough_data_yet(self):
        m = linkstats.LinkMonitor()
        self.assertIsNone(m.health())
        m.add(linkstats.LinkSample(0, 0, 0, 12.0))
        h = m.health()                         # one sample is enough for a round-trip time, not for a rate
        self.assertEqual((h.delivered_mbps, h.retx_pct, h.rtt_ms), (None, None, 12.0))

    def test_rate_needs_a_long_enough_window_to_be_honest(self):
        m = linkstats.LinkMonitor()
        self.feed(m, 5, 250_000)               # only 5 s of data: bursts would make a rate meaningless
        self.assertIsNone(m.health().delivered_mbps)
        self.feed(m, 20, 250_000, start=6.0, bytes0=6 * 250_000)
        self.assertAlmostEqual(m.health().delivered_mbps, 2.0, delta=0.05)

    def test_readings_go_stale_once_the_samples_stop(self):
        m = linkstats.LinkMonitor()
        self.feed(m, 30, 250_000)
        self.assertIsNotNone(m.health(now=30.0))
        self.assertIsNone(m.health(now=30.0 + m.STALE_AFTER + 1))    # the stream connection is gone: no "good" link
        self.assertIsNotNone(m.health())                              # without a clock the last readings are served

    def test_retransmits_that_cannot_be_measured_are_left_out(self):
        m = linkstats.LinkMonitor()
        for i in range(31):                    # a native cast: byte counts from the server, nothing from TCP
            m.add(linkstats.LinkSample(float(i), 250_000 * i, None, None))
        h = m.health()
        self.assertAlmostEqual(h.delivered_mbps, 2.0, delta=0.05)
        self.assertIsNone(h.retx_pct)
        self.assertIsNone(h.rtt_ms)

    def test_counter_reset_starts_over_instead_of_going_negative(self):
        m = linkstats.LinkMonitor()
        self.feed(m, 20, 250_000)
        self.feed(m, 5, 250_000, start=21.0)   # new connection: counters back near zero
        h = m.health()
        self.assertGreaterEqual(h.delivered_mbps or 0, 0)


class Coordinator(unittest.TestCase):
    def make(self, tv_states, byte_rate=1.2 * MB, with_reader=True):
        clock = {"t": 0.0}
        states = iter(tv_states)

        class Reader:
            connected = with_reader

            def open(self, timeout=0):
                return True

            def poll(self):
                s = next(states, tv_states[-1])
                return TvStatus(s[0], s[1], 1000.0)

            def close(self):
                pass

        sent = {"b": 0}

        def read_link():
            sent["b"] += int(byte_rate)
            # the TV stays 30 s ahead of the position the tests set (100 + t)
            return linkstats.LinkSample(clock["t"], sent["b"], 0, 18.0, int((130 + clock["t"]) * MB))

        tele = telemetry.Telemetry(Reader(), read_link, CURVE, clock=lambda: clock["t"],
                                   k=1.05)
        return tele, clock

    def test_snapshot_combines_tv_link_and_buffer(self):
        tele, clock = self.make([(i, "PLAYING") for i in range(1, 40)])
        snap = None
        for i in range(1, 25):
            clock["t"] = float(i)
            tele.position = 100.0 + i
            snap = tele.step()
        self.assertEqual(snap.tv_state, "PLAYING")
        self.assertEqual(snap.tv_position, 24)
        self.assertAlmostEqual(snap.delivered_mbps, 1.2 * 8, delta=0.3)
        self.assertAlmostEqual(snap.needed_mbps, 8.4, delta=0.1)       # 1 MB/s * K(1.05)
        self.assertIsNotNone(snap.buffer_s)
        self.assertEqual(snap.rtt_ms, 18.0)

    def test_a_seek_shows_up_as_refilling_then_clears(self):
        tele, clock = self.make([(i, "PLAYING") for i in range(1, 80)])
        for i in range(1, 6):
            clock["t"] = float(i)
            tele.position = 100.0 + i
            tele.step()
        clock["t"] = 6.0
        tele.position = 400.0                                              # user jumped forward
        snap = tele.step()
        self.assertTrue(snap.refilling)
        self.assertEqual(snap.verdict, "refilling")
        for i in range(1, int(telemetry.REFILL_S) + 3):                    # playback carries on normally afterwards
            clock["t"] = 6.0 + i
            tele.position = 400.0 + i
            snap = tele.step()
        self.assertFalse(snap.refilling)

    def test_tv_buffering_forces_an_empty_buffer(self):
        tele, clock = self.make([(1, "PLAYING"), (2, "PLAYING"), (3, "BUFFERING")])
        for i in range(1, 4):
            clock["t"] = float(i)
            tele.position = 100.0 + i
            snap = tele.step()
        self.assertEqual(snap.tv_state, "BUFFERING")
        self.assertEqual(snap.buffer_s, 0.0)
        self.assertEqual(snap.verdict, "poor")

    def test_works_without_a_curve_or_reader(self):
        class Dead:
            connected = False

            def open(self, timeout=0):
                return False

            def poll(self):
                return None

            def close(self):
                pass

        tele = telemetry.Telemetry(Dead(), lambda: linkstats.LinkSample(0, 1, 0, None), None)
        snap = tele.step()
        self.assertIsNone(snap.buffer_s)
        self.assertIsNone(snap.tv_state)

    def test_k_is_a_parameter(self):
        tele = telemetry.Telemetry(self.make([(1, "PLAYING")])[0].reader, lambda: None, CURVE, k=1.0)
        tele.position = 100.0
        self.assertAlmostEqual(tele.step().needed_mbps, 8.0, delta=0.01)          # 1 MB/s, nothing added

    def test_a_native_session_is_read_through_its_own_counters(self):
        class Session:
            sent, reached, pos = 0, 0, 100.0

            def tv_status(self):
                return TvStatus(self.pos, "PLAYING", 1000.0)

            def link_sample(self):
                return linkstats.LinkSample(clock["t"], self.sent, None, None, self.reached)

        clock = {"t": 0.0}
        session = Session()
        reader = telemetry.SessionReader(session)
        tele = telemetry.Telemetry(reader, session.link_sample, CURVE, clock=lambda: clock["t"], k=telemetry.NATIVE_K)
        for i in range(1, 25):
            clock["t"] = float(i)
            session.pos = 100.0 + i
            session.sent += 1_200_000
            session.reached = int((130 + i) * MB)                                 # the TV stays 30 s ahead
            tele.position = session.pos
            snap = tele.step()
        self.assertEqual((snap.tv_state, snap.tv_position), ("PLAYING", 124.0))
        self.assertAlmostEqual(snap.buffer_s, 30.0, delta=0.1)                    # exact, not an estimate from counts
        self.assertAlmostEqual(snap.delivered_mbps, 9.6, delta=0.3)
        self.assertAlmostEqual(snap.needed_mbps, 8.0, delta=0.01)                 # K = 1: the file itself
        self.assertIsNone(snap.retx_pct)
        self.assertIsNone(snap.rtt_ms)
        self.assertEqual(snap.verdict, "good")
        tele.stop()

    def test_a_cast_on_the_fly_reports_its_buffer_exactly(self):
        class Session:
            ahead, pos = 30.0, 100.0

            def tv_status(self):
                return TvStatus(self.pos, "PLAYING", 1000.0)

            def link_sample(self):
                return linkstats.LinkSample(clock["t"], 1_200_000 * int(clock["t"]), None, None, ahead=self.ahead)

        clock = {"t": 0.0}
        session = Session()
        tele = telemetry.Telemetry(telemetry.SessionReader(session), session.link_sample, None,
                                   clock=lambda: clock["t"], k=telemetry.NATIVE_K)
        for i in range(1, 25):
            clock["t"] = float(i)
            session.pos = tele.position = 100.0 + i
            snap = tele.step()
        self.assertEqual(snap.buffer_s, 30.0)                                     # the number the session gave, no smoothing
        self.assertIsNone(snap.needed_mbps)                                       # no bitrate profile cast on the fly
        self.assertAlmostEqual(snap.delivered_mbps, 9.6, delta=0.3)
        self.assertFalse(snap.refilling)
        self.assertEqual(snap.verdict, "good")
        session.ahead = 3.0                                                       # the encoder cannot keep up
        self.assertEqual(tele.step().buffer_s, 3.0)
        self.assertEqual(tele.step().verdict, "poor")
        session.ahead = 0.0
        self.assertEqual(tele.step().buffer_s, 0.0)
        session.ahead = 500.0
        self.assertEqual(tele.step().buffer_s, telemetry.MAX_BUFFER_S)            # capped, like the estimate

    def test_a_seek_on_the_fly_is_not_refilling(self):
        """The server counts the segments the TV holds: after a jump the figure simply drops, and that is the truth."""
        ahead = {"v": 40.0}
        clock = {"t": 0.0}
        tele = telemetry.Telemetry(self.make([(1, "PLAYING")])[0].reader,
                                   lambda: linkstats.LinkSample(clock["t"], 1, None, None, ahead=ahead["v"]),
                                   CURVE, clock=lambda: clock["t"])
        tele.position = 100.0
        clock["t"] = 1.0
        self.assertEqual(tele.step().buffer_s, 40.0)
        tele.position, ahead["v"], clock["t"] = 900.0, 0.0, 2.0                    # far jump: the estimator would re-anchor
        snap = tele.step()
        self.assertEqual(snap.buffer_s, 0.0)
        self.assertFalse(snap.refilling)
        self.assertIsNone(tele.estimator.value, "the byte-curve estimator is not used")

    def test_ahead_defaults_to_none_so_the_other_samples_are_unchanged(self):
        self.assertIsNone(linkstats.LinkSample(0.0, 1, 0, None).ahead)
        self.assertIsNone(linkstats.LinkSample(0.0, 1, None, None, 5).ahead)

    def test_background_thread_updates_and_stops(self):
        tele, clock = self.make([(1, "PLAYING")])
        tele.INTERVAL = 0.05
        tele.start()
        import time
        deadline = time.time() + 3
        while tele.latest.tv_state is None and time.time() < deadline:
            time.sleep(0.05)
        tele.stop()
        self.assertEqual(tele.latest.tv_state, "PLAYING")
        self.assertFalse(tele._thread.is_alive())


if __name__ == "__main__":
    unittest.main()
