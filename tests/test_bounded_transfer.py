import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import bounded_transfer
from scripts.bounded_transfer import MAX_CONTENT_BYTES, batches, encode_batch


class BoundedTransferTests(unittest.TestCase):
    def test_full_scale_inventory_is_complete_ordered_and_bounded(self):
        names = [f"data/listings/{n:032x}/000.jpg" for n in range(409163)]
        names += ["data/otz.db", "data/photo_manifest.json"]
        chunks = list(batches(names))
        self.assertEqual([n for chunk in chunks for n in chunk], names)
        self.assertEqual(len(chunks), 410)
        self.assertTrue(all(len(c) <= 1000 and len(encode_batch(c)) <= 128 * 1024 for c in chunks))

    def test_byte_limit_and_unicode_count_encoded_bytes(self):
        names = [f"data/listings/дом-{n}/0.jpg" for n in range(25)]
        chunks = list(batches(names, max_files=20, max_bytes=100))
        self.assertEqual([n for c in chunks for n in c], names)
        self.assertTrue(all(len(encode_batch(c)) <= 100 for c in chunks))

    def test_content_limit_splits_batches_by_file_size(self):
        names = [f"data/listings/{n:03d}.jpg" for n in range(6)]
        sizes = {name: 400 for name in names}
        chunks = list(batches(names, max_files=1000, sizes=sizes, max_content_bytes=1000))
        self.assertEqual([n for c in chunks for n in c], names)
        self.assertEqual([len(c) for c in chunks], [2, 2, 2])  # 2 x 400 fits, 3 x 400 does not

    def test_a_file_larger_than_the_content_limit_is_refused_not_batched(self):
        names = ["data/a.jpg", "data/otz.db", "data/b.jpg"]
        sizes = {"data/a.jpg": 10, "data/otz.db": 5000, "data/b.jpg": 10}
        with self.assertRaisesRegex(ValueError, "^file_over_batch_cap$"):
            list(batches(names, sizes=sizes, max_content_bytes=1000))

    def test_the_default_content_limit_is_4_gib_and_splits_batches_by_size(self):
        self.assertEqual(MAX_CONTENT_BYTES, 4 * 1024**3)
        names = [f"data/listings/{n:03d}.jpg" for n in range(3)]
        sizes = {name: 3 * 1024**3 // 2 for name in names}
        self.assertEqual([len(c) for c in batches(names, sizes=sizes)], [2, 1])  # 3 GiB, then 1.5
        sizes = {name: MAX_CONTENT_BYTES for name in names}  # exactly the cap is still accepted
        self.assertEqual([len(c) for c in batches(names, sizes=sizes)], [1, 1, 1])
        sizes = {name: MAX_CONTENT_BYTES + 1 for name in names}
        with self.assertRaisesRegex(ValueError, "^file_over_batch_cap$"):
            list(batches(names, sizes=sizes))

    def test_the_default_cap_is_read_when_the_call_runs(self):
        # The default is read from the module when batches() runs, so a patched value applies here.
        with mock.patch.object(bounded_transfer, "MAX_CONTENT_BYTES", 100):
            self.assertEqual(list(batches(["a", "b"], sizes={"a": 60, "b": 60})), [["a"], ["b"]])
            with self.assertRaisesRegex(ValueError, "^file_over_batch_cap$"):
                list(batches(["c"], sizes={"c": 101}))

    def test_a_database_sized_file_is_batched_within_the_default_cap(self):
        # Sizes are the manifest's, as the receiver passes them; no bytes are read. The staged
        # database of 2026-09-10 was 1,496,510,464 bytes (1.39 GiB).
        names = ["data/listings/0.jpg", "data/otz.db", "data/listings/1.jpg"]
        sizes = {
            "data/listings/0.jpg": 3 * 1024**3,
            "data/otz.db": 1_496_510_464,
            "data/listings/1.jpg": 10 * 1024**2,
        }
        chunks = list(batches(names, sizes=sizes))
        # 3 GiB plus the database is over 4 GiB, so the database opens a new batch that the small
        # file after it still fits in. Nothing is refused, and every batch is within the cap.
        self.assertEqual(chunks, [["data/listings/0.jpg"], ["data/otz.db", "data/listings/1.jpg"]])
        self.assertTrue(all(sum(sizes[n] for n in c) <= MAX_CONTENT_BYTES for c in chunks))

    def test_content_limit_needs_a_size_for_every_name(self):
        with self.assertRaises(ValueError):
            list(batches(["data/a.jpg"], sizes={}, max_content_bytes=1000))
        with self.assertRaises(ValueError):
            list(batches(["data/a.jpg"], sizes={"data/a.jpg": -1}, max_content_bytes=1000))

    def test_no_empty_batch(self):
        self.assertEqual(list(batches([])), [])
        self.assertEqual(list(batches(["data/otz.db"])), [["data/otz.db"]])

    def test_unsafe_duplicate_and_oversize_members_are_rejected(self):
        for names in [["/etc/passwd"], ["data/../.env"], ["data/a\0b"], ["data//file"], ["data/a", "data/a"]]:
            with self.subTest(names=names), self.assertRaises(ValueError):
                list(batches(names))
        with self.assertRaises(ValueError):
            list(batches(["data/long-name"], max_bytes=3))

    @unittest.skipUnless(Path("/usr/bin/rsync").exists(), "requires /usr/bin/rsync")
    def test_real_openrsync_batches_copy_exact_allowlist(self):
        with tempfile.TemporaryDirectory(prefix='renfinder-batched-rsync-test-') as directory:
            root = Path(directory)
            source = root / 'source'
            destination = root / 'destination'
            source.mkdir()
            destination.mkdir()
            names = [f'data/listings/{n:08d}/0.jpg' for n in range(1505)]
            for name in names:
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(('synthetic-only:' + name).encode())
            (source / '.env').write_text('SYNTHETIC_EXCLUDED=1\n')
            (source / 'test.session').write_bytes(b'SYNTHETIC_EXCLUDED')
            for group in batches(names):
                listing = root / 'batch.files'
                listing.write_bytes(encode_batch(group))
                subprocess.run(['/usr/bin/rsync', '-rt', '-I', '-0', '--files-from', str(listing), str(source) + '/', str(destination) + '/'], check=True, capture_output=True, timeout=45, env={'PATH':'/usr/bin:/bin', 'LANG':'C'})
            self.assertEqual({p.relative_to(destination).as_posix() for p in destination.rglob('*') if p.is_file()}, set(names))
            for name in names:
                self.assertEqual(hashlib.sha256((source/name).read_bytes()).digest(), hashlib.sha256((destination/name).read_bytes()).digest())


if __name__ == "__main__":
    unittest.main()
