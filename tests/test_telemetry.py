import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from castmymovie import linkstats, telemetry  # noqa: E402
from castmymovie.castlink import TvStatus  # noqa: E402

MB = 1_000_000
CURVE = [t * MB for t in range(1000)]          # a movie at a steady 1 MB per second

NETTOP = """,bytes_out,re-tx,rtt_avg,
tcp4 10.0.0.2:49537<->10.0.0.9:8009,2041,0,250.00 ms,
tcp4 10.0.0.2:49511<->10.0.0.9:46376,59672535,678044,33.34 ms,
tcp6 *.49511<->*.*,,,,
tcp4 10.0.0.2:49511<->10.0.0.77:5000,111,0,1.00 ms,
"""


class Estimator(unittest.TestCase):
    def est(self):
        e = telemetry.BufferEstimator(CURVE, k=1.0)
        e.begin(0.0)
        return e

    def test_bytes_ahead_of_the_tv_become_seconds(self):
        e = self.est()
        self.assertAlmostEqual(e.update(10.0, 40 * MB), 30.0)        # 40 s delivered, 10 s played

    def test_flat_while_paused(self):
        e = self.est()
        e.update(10.0, 40 * MB)
        for _ in range(30):
            v = e.update(10.0, 40 * MB)
        self.assertAlmostEqual(v, 30.0, places=2)

    def test_declines_while_playing_and_jumps_on_refill(self):
        e = self.est()
        e.update(10.0, 40 * MB)
        for t in range(11, 16):
            v = e.update(float(t), 40 * MB)
        self.assertLess(v, 30.0)
        refilled = [e.update(16.0, 46 * MB) for _ in range(20)][-1]   # +6 s of data arrives
        self.assertGreater(refilled, v)
        self.assertAlmostEqual(refilled, 30.0, delta=0.1)

    def test_smoothing_damps_single_spikes(self):
        e = self.est()
        e.update(10.0, 40 * MB)
        v = e.update(10.0, 100 * MB)   # raw would be 90, but we only move part of the way
        self.assertTrue(30 < v < 60, v)

    def test_stall_means_empty_buffer(self):
        e = self.est()
        e.update(10.0, 40 * MB)
        self.assertEqual(e.update(12.0, 40 * MB, stalled=True), 0.0)
        later = e.update(12.0, 44 * MB)                                 # 4 s of new data, anchored at empty
        self.assertTrue(0 < later < 4, later)

    def test_seek_reanchors(self):
        e = self.est()
        e.update(10.0, 40 * MB)
        self.assertEqual(e.update(500.0, 41 * MB), 0.0)                # position jumped: the TV flushed

    def test_new_connection_reanchors(self):
        e = self.est()
        e.update(10.0, 40 * MB)
        self.assertEqual(e.update(11.0, 1 * MB), 0.0)                   # byte counter went backwards

    def test_reanchor_is_reported_so_the_ui_can_say_refilling(self):
        e = self.est()
        e.update(10.0, 40 * MB)
        self.assertFalse(e.reanchored)
        e.update(500.0, 41 * MB)
        self.assertTrue(e.reanchored)
        e.update(501.0, 41 * MB)
        self.assertFalse(e.reanchored)

    def test_value_is_clamped(self):
        e = self.est()
        self.assertEqual(e.update(1.0, 900 * MB), telemetry.MAX_BUFFER_S)
        self.assertGreaterEqual(self.est().update(50.0, 1 * MB), 0.0)   # more played than sent -> 0, never negative

    def test_k_scales_the_conversion(self):
        e = telemetry.BufferEstimator(CURVE, k=1.05)
        e.begin(0.0)
        self.assertAlmostEqual(e.update(10.0, 42 * MB), 30.0, delta=0.01)   # 42 MB / 1.05 = 40 s

    def test_tiny_curve_gives_no_estimate(self):
        self.assertIsNone(telemetry.BufferEstimator([0, 5]).update(1.0, 10))


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


class NetTop(unittest.TestCase):
    def test_real_nettop_output_picks_only_the_video_stream(self):
        # Captured from a live cast: per-process summary row, loopback rc connection, the TV's :8009 control
        # connection (which had retransmits) and the actual stream connection.
        text = (Path(__file__).resolve().parent / "fixtures" / "nettop_real.txt").read_text()
        out, retx, rtt = linkstats.parse_nettop(text, "10.0.0.9")
        self.assertEqual((out, retx), (6269935, 0))
        self.assertAlmostEqual(rtt, 23.22)

    def test_sums_stream_connections_and_ignores_control_and_other_hosts(self):
        out, retx, rtt = linkstats.parse_nettop(NETTOP, "10.0.0.9")
        self.assertEqual((out, retx), (59672535, 678044))
        self.assertAlmostEqual(rtt, 33.34)

    def test_no_connection_or_no_header(self):
        self.assertIsNone(linkstats.parse_nettop(NETTOP, "10.9.9.9"))
        self.assertIsNone(linkstats.parse_nettop("", "10.0.0.9"))
        self.assertIsNone(linkstats.parse_nettop("garbage\nmore garbage", "10.0.0.9"))

    def test_columns_are_found_by_name_not_position(self):
        shuffled = ",rtt_avg,bytes_out,re-tx,\ntcp4 1.1.1.1:1<->10.0.0.9:2,5.00 ms,1000,10,\n"
        self.assertEqual(linkstats.parse_nettop(shuffled, "10.0.0.9"), (1000, 10, 5.0))

    def test_read_sample_survives_a_missing_tool(self):
        def boom(*a, **k):
            raise OSError("no nettop")
        self.assertIsNone(linkstats.read_sample(1, "10.0.0.9", run=boom))


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

        def read_link(pid, host):
            sent["b"] += int(byte_rate)
            return linkstats.LinkSample(clock["t"], sent["b"], 0, 18.0)

        tele = telemetry.Telemetry(1, "tv", CURVE, reader=Reader(), read_link=read_link, clock=lambda: clock["t"])
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

        tele = telemetry.Telemetry(1, "tv", None, reader=Dead(),
                                   read_link=lambda p, h: linkstats.LinkSample(0, 1, 0, None))
        snap = tele.step()
        self.assertIsNone(snap.buffer_s)
        self.assertIsNone(snap.tv_state)

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
