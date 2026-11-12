"""Unit tests for installer/patch_iisu.py's structural class discovery and
MediaBridge token substitution (pure text work on synthetic smali, no
apktool/JRE/SDK involved)."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "installer"))

import patch_iisu  # noqa: E402; path set up above


def _helper_smali(class_name: str, signatures=patch_iisu.ASSET_HELPER_METHODS) -> str:
    body = "\n".join(f"{sig}\n    return-void\n.end method\n" for sig in signatures)
    return f".class public final L{class_name};\n.super Ljava/lang/Object;\n\n{body}"


class RedirectStartActivityTests(unittest.TestCase):
    """The coordinator's dispatch method, as iiSU 7.5-prerelease2 compiles it
    (class ax2, method k()): one call passes an options Bundle, one doesn't."""

    COORDINATOR = (
        ".class public final Lax2;\n"
        ".method public final k()V\n"
        '    const-string v4, "GameLaunchCoordinator"\n'
        "    if-eqz v0, :cond_5\n"
        "    invoke-virtual {v1, p0, v0}, Landroid/content/Context;->startActivity(Landroid/content/Intent;Landroid/os/Bundle;)V\n"
        "    return-void\n"
        "    :cond_5\n"
        "    invoke-virtual {v1, p0}, Landroid/content/Context;->startActivity(Landroid/content/Intent;)V\n"
        "    return-void\n"
        ".end method\n"
    )

    def test_the_options_bundle_goes_along_when_there_was_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ax2.smali"
            path.write_text(self.COORDINATOR, encoding="utf-8")
            self.assertEqual(patch_iisu.patch_all_start_activity_calls(path), 2)
            text = path.read_text(encoding="utf-8")
        self.assertNotIn("->startActivity(", text)
        self.assertIn(
            "    invoke-static {v1, p0, v0}, Lcom/iisulauncher/pcbridge/LaunchBridge;->launch("
            "Landroid/content/Context;Landroid/content/Intent;Landroid/os/Bundle;)V\n", text,
        )
        self.assertIn(
            "    invoke-static {v1, p0}, Lcom/iisulauncher/pcbridge/LaunchBridge;->launch("
            "Landroid/content/Context;Landroid/content/Intent;)V\n", text,
        )

    def test_both_launch_forms_exist_in_the_injected_class(self):
        smali = (patch_iisu.SMALI_PATCH_DIR / "LaunchBridge.smali").read_text(encoding="utf-8")
        for signature in ("launch(Landroid/content/Context;Landroid/content/Intent;)V",
                          "launch(Landroid/content/Context;Landroid/content/Intent;Landroid/os/Bundle;)V"):
            with self.subTest(signature=signature):
                self.assertIn(f".method public static {signature}", smali)


class FindAssetHelperTests(unittest.TestCase):
    def _tree(self, tmp: str, files: dict[str, str]) -> Path:
        root = Path(tmp)
        for name, text in files.items():
            (root / name).write_text(text, encoding="utf-8")
        return root

    def test_finds_the_single_class_with_every_signature(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(tmp, {"xa7.smali": _helper_smali("xa7"), "other.smali": ".class public final Lother;\n"})
            self.assertEqual(patch_iisu.find_asset_helper_class(root), "xa7")

    def test_partial_signature_set_is_not_a_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(tmp, {"half.smali": _helper_smali("half", patch_iisu.ASSET_HELPER_METHODS[:3])})
            self.assertIsNone(patch_iisu.find_asset_helper_class(root))

    def test_ambiguous_match_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(tmp, {"a.smali": _helper_smali("a"), "b.smali": _helper_smali("b")})
            self.assertIsNone(patch_iisu.find_asset_helper_class(root))


class InjectLaunchBridgeTests(unittest.TestCase):
    RECEIVER = (
        ".class public final Lcom/iisulauncher/pcbridge/MediaBridgeReceiver;\n"
        "    sget-object v0, LIISUPC_HOLDER;->iisupcHolder:LIISUPC_HOLDER;\n"
        "    # IISUPC_ASSET_INDEX_BEGIN\n"
        "    invoke-static {p3, v9}, LIISUPC_ASSETS;->n(Ljava/io/File;Ljava/lang/String;)Ljava/io/File;\n"
        "    # IISUPC_ASSET_INDEX_END\n"
        "    return-void\n"
    )

    def _inject(self, asset_helper):
        with tempfile.TemporaryDirectory() as tmp:
            patch_dir = Path(tmp) / "smali_patch"
            patch_dir.mkdir()
            (patch_dir / "MediaBridgeReceiver.smali").write_text(self.RECEIVER, encoding="utf-8")
            out = Path(tmp) / "decompiled"
            with patch.object(patch_iisu, "SMALI_PATCH_DIR", patch_dir):
                patch_iisu.inject_launch_bridge(out, "jq6", asset_helper)
            return (out / "smali" / patch_iisu.BRIDGE_PACKAGE_SMALI_DIR / "MediaBridgeReceiver.smali").read_text(encoding="utf-8")

    def test_tokens_are_replaced_with_discovered_classes(self):
        text = self._inject("xa7")
        self.assertIn("Ljq6;->iisupcHolder:Ljq6;", text)
        self.assertIn("Lxa7;->n(", text)
        self.assertNotIn("LIISUPC_", text)

    def test_missing_asset_helper_strips_the_index_block(self):
        text = self._inject(None)
        self.assertIn("Ljq6;->iisupcHolder", text)
        self.assertNotIn("IISUPC_ASSET", text)
        self.assertNotIn("->n(", text)
        self.assertIn("return-void", text)


class MainActivityRefreshTests(unittest.TestCase):
    def test_detects_the_refresh_entry_point(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp) / "smali" / "com" / "iisulauncher" / "launcher"
            pkg.mkdir(parents=True)
            (pkg / "MainActivity.smali").write_text(patch_iisu.MAIN_ACTIVITY_REFRESH_METHOD + "\n", encoding="utf-8")
            self.assertTrue(patch_iisu.has_main_activity_refresh(Path(tmp)))

    def test_missing_entry_point(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "MainActivity.smali").write_text(".class public Lx;\n", encoding="utf-8")
            self.assertFalse(patch_iisu.has_main_activity_refresh(Path(tmp)))


if __name__ == "__main__":
    unittest.main()


class EnableLargeHeapTests(unittest.TestCase):
    """iiSU's Coil memory cache is sized from the large memory class once
    largeHeap is set (576 MB instead of 192 MB on the AVD)."""

    # The <application> tag as apktool decodes it from iiSU Alpha 7.5.
    MANIFEST = (
        '<?xml version="1.0" encoding="utf-8" standalone="no"?>\n'
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android" package="com.iisulauncher">\n'
        '    <application android:allowBackup="false" android:appCategory="game"\n'
        '        android:extractNativeLibs="false" android:label="@string/app_name"\n'
        '        android:name="com.iisulauncher.LairLauncherApplication">\n'
        '        <activity android:name="com.iisulauncher.MainActivity"/>\n'
        '    </application>\n'
        '</manifest>\n'
    )

    def _run(self, manifest: str) -> tuple[bool, str]:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "AndroidManifest.xml"
            path.write_text(manifest, encoding="utf-8")
            changed = patch_iisu.enable_large_heap(Path(tmp))
            return changed, path.read_text(encoding="utf-8")

    def test_large_heap_is_added_to_the_application_tag(self):
        changed, text = self._run(self.MANIFEST)
        self.assertTrue(changed)
        self.assertIn('<application android:largeHeap="true" android:allowBackup="false"', text)
        self.assertEqual(text.count("largeHeap"), 1)
        self.assertIn('<activity android:name="com.iisulauncher.MainActivity"/>', text)

    def test_running_it_again_changes_nothing(self):
        _, once = self._run(self.MANIFEST)
        changed, twice = self._run(once)
        self.assertFalse(changed)
        self.assertEqual(once, twice)

    def test_an_explicit_false_is_turned_on(self):
        manifest = self.MANIFEST.replace("<application ", '<application android:largeHeap="false" ')
        changed, text = self._run(manifest)
        self.assertTrue(changed)
        self.assertIn('android:largeHeap="true"', text)
        self.assertNotIn('largeHeap="false"', text)

    def test_a_manifest_without_an_application_tag_is_an_error(self):
        with self.assertRaises(RuntimeError):
            self._run('<manifest package="x"></manifest>\n')


class BrowserDecodeParallelismTests(unittest.TestCase):
    """The home screen's cover loader (j80 in Alpha 7.5) starts at most 2
    decodes at once; the patch raises that limit in both places it's used."""

    # Trimmed from Alpha 7.5's j80.smali: the dispatch loop and the free-slot count.
    LOADER = (
        ".class public final Lj80;\n"
        ".super Ljava/lang/Object;\n\n"
        ".method public final K(Ljava/util/ArrayList;)V\n"
        "    iget-object v6, v0, Lj80;->j:Ljava/util/LinkedHashSet;\n\n"
        "    invoke-interface {v6}, Ljava/util/Set;->size()I\n\n"
        "    .line 12\n"
        "    move-result v6\n\n"
        "    rsub-int/lit8 v6, v6, 0x2\n\n"
        "    invoke-static {v6, v3}, Liz2;->r(II)I\n"
        ".end method\n\n"
        ".method public final y()V\n"
        "    new-instance v0, Ljava/util/ArrayList;\n\n"
        "    .line 2\n"
        "    .line 3\n"
        "    const/4 v1, 0x2\n\n"
        "    .line 4\n"
        "    invoke-direct {v0, v1}, Ljava/util/ArrayList;-><init>(I)V\n\n"
        "    iget-object v3, p0, Lj80;->j:Ljava/util/LinkedHashSet;\n"
        "    invoke-interface {v3}, Ljava/util/Set;->size()I\n"
        "    move-result v3\n"
        "    if-ge v3, v1, :cond_3\n"
        ".end method\n\n"
        ".method public final z()Ljava/lang/String;\n"
        '    const-string v0, "inFlight="\n'
        "    const/4 v2, 0x2\n"
        ".end method\n"
    )

    def _tree(self, tmp: str, files: dict[str, str]) -> Path:
        root = Path(tmp)
        for name, text in files.items():
            path = root / "smali" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return root

    def test_both_uses_of_the_limit_are_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(tmp, {"j80.smali": self.LOADER, "other.smali": ".class public final Lother;\n"})
            with patch("builtins.print"):
                self.assertTrue(patch_iisu.raise_browser_decode_parallelism(root))
            text = (root / "smali" / "j80.smali").read_text(encoding="utf-8")
        self.assertIn("rsub-int/lit8 v6, v6, 0x6", text)
        self.assertIn("    const/4 v1, 0x6\n\n    .line 4\n    invoke-direct {v0, v1}, Ljava/util/ArrayList;-><init>(I)V", text)
        # unrelated constants in the same class are untouched
        self.assertIn("const/4 v2, 0x2", text)
        self.assertEqual(text.count("0x6"), 2)

    def test_an_already_patched_loader_is_left_alone_quietly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(tmp, {"j80.smali": self.LOADER})
            with patch("builtins.print"):
                patch_iisu.raise_browser_decode_parallelism(root)
            once = (root / "smali" / "j80.smali").read_text(encoding="utf-8")
            with patch("builtins.print") as printed:
                self.assertFalse(patch_iisu.raise_browser_decode_parallelism(root))
            self.assertEqual((root / "smali" / "j80.smali").read_text(encoding="utf-8"), once)
            printed.assert_not_called()

    def test_an_unfamiliar_build_is_left_unchanged(self):
        changed = self.LOADER.replace("rsub-int/lit8 v6, v6, 0x2", "rsub-int/lit8 v6, v6, 0x3")
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(tmp, {"j80.smali": changed})
            with patch("builtins.print") as printed:
                self.assertFalse(patch_iisu.raise_browser_decode_parallelism(root))
            self.assertEqual((root / "smali" / "j80.smali").read_text(encoding="utf-8"), changed)
        self.assertIn("leaving it as is", printed.call_args.args[0])

    def test_no_loader_class_is_left_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(tmp, {"other.smali": ".class public final Lother;\n"})
            with patch("builtins.print") as printed:
                self.assertFalse(patch_iisu.raise_browser_decode_parallelism(root))
        self.assertIn("couldn't find", printed.call_args.args[0])

    def test_the_limit_must_fit_the_instruction(self):
        with self.assertRaises(ValueError):
            patch_iisu.raise_browser_decode_parallelism(Path("."), limit=8)


class WidenBrowserPrefetchTests(unittest.TestCase):
    """iiSU's grid window (s13.W in Alpha 7.5) loads covers one row beyond the
    screen (one screen length for free-form layouts); the patch widens both."""

    # Trimmed from Alpha 7.5's s13.W, .line markers included as apktool writes them.
    WINDOW = (
        ".class public final Ls13;\n"
        ".super Ljava/lang/Object;\n\n"
        ".method public static W(Ls13;)Lyd9;\n"
        "    .locals 13\n\n"
        "    const/high16 v5, 0x3f800000    # 1.0f\n\n"
        "    if-eqz v4, :cond_14\n\n"
        "    .line 206\n"
        "    iget v1, v1, Ll13;->a:F\n\n"
        "    .line 207\n"
        "    .line 208\n"
        "    mul-float/2addr v5, v1\n\n"
        "    :cond_14\n"
        "    invoke-static {v3, v4}, Ljava/lang/Math;->floor(D)D\n\n"
        "    invoke-static {v3, v4}, Ljava/lang/Math;->ceil(D)D\n\n"
        "    .line 361\n"
        "    sub-int/2addr v0, v6\n\n"
        "    .line 362\n"
        "    mul-int/2addr v0, v2\n\n"
        "    .line 363\n"
        "    if-gez v0, :cond_17\n\n"
        "    .line 364\n"
        "    .line 365\n"
        "    goto :goto_d\n\n"
        "    .line 366\n"
        "    :cond_17\n"
        "    move v9, v0\n\n"
        "    .line 367\n"
        "    :goto_d\n"
        "    add-int/lit8 v1, v1, 0x2\n\n"
        "    .line 368\n"
        "    .line 369\n"
        "    mul-int/2addr v1, v2\n\n"
        "    invoke-direct {v5, v3, v4, v9, v1}, Lyd9;-><init>(IIII)V\n\n"
        "    return-object v5\n"
        ".end method\n"
    )

    def _run(self, files: dict[str, str], **kwargs) -> tuple[bool, dict[str, str], unittest.mock.MagicMock]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, text in files.items():
                (root / name).write_text(text, encoding="utf-8")
            with patch("builtins.print") as printed:
                changed = patch_iisu.widen_browser_prefetch(root, **kwargs)
            return changed, {name: (root / name).read_text(encoding="utf-8") for name in files}, printed

    def test_uniform_and_free_form_grids_are_widened(self):
        changed, out, printed = self._run({"s13.smali": self.WINDOW})
        text = out["s13.smali"]
        self.assertTrue(changed)
        self.assertIn("    .line 361\n    add-int/lit8 v0, v0, -0x6\n\n    .line 362\n    mul-int/2addr v0, v2", text)
        self.assertIn("    :goto_d\n    add-int/lit8 v1, v1, 0x7\n", text)
        self.assertIn("iget v1, v1, Ll13;->a:F\n\n    .line 207\n    .line 208\n    const/high16 v5, 0x40000000\n\n    mul-float/2addr v5, v1", text)
        self.assertNotIn("sub-int/2addr v0, v6", text)
        # the method's opening 1.0f is still there for its other branches
        self.assertIn("const/high16 v5, 0x3f800000    # 1.0f", text)
        printed.assert_not_called()

    def test_custom_amounts(self):
        _, out, _ = self._run({"s13.smali": self.WINDOW}, rows=4, screens=3.0)
        text = out["s13.smali"]
        self.assertIn("add-int/lit8 v0, v0, -0x4", text)
        self.assertIn("add-int/lit8 v1, v1, 0x5", text)
        self.assertIn("const/high16 v5, 0x40400000", text)

    def test_running_it_again_changes_nothing_and_says_nothing(self):
        _, out, _ = self._run({"s13.smali": self.WINDOW})
        changed, again, printed = self._run(out)
        self.assertFalse(changed)
        self.assertEqual(again, out)
        printed.assert_not_called()

    def test_an_unfamiliar_build_is_left_unchanged(self):
        changed_build = self.WINDOW.replace("add-int/lit8 v1, v1, 0x2", "add-int/lit8 v1, v1, 0x3")
        changed, out, printed = self._run({"s13.smali": changed_build})
        self.assertFalse(changed)
        self.assertEqual(out["s13.smali"], changed_build)
        self.assertIn("leaving how far ahead", printed.call_args.args[0])

    def test_two_lookalike_methods_are_left_unchanged(self):
        changed, out, _ = self._run({"s13.smali": self.WINDOW, "t13.smali": self.WINDOW.replace("Ls13;", "Lt13;")})
        self.assertFalse(changed)
        self.assertEqual(out["s13.smali"], self.WINDOW)

    def test_without_the_free_form_margin_only_uniform_grids_change(self):
        no_margin = self.WINDOW.replace("    mul-float/2addr v5, v1\n", "")
        changed, out, printed = self._run({"s13.smali": no_margin})
        self.assertTrue(changed)
        self.assertIn("add-int/lit8 v0, v0, -0x6", out["s13.smali"])
        self.assertNotIn("0x40000000", out["s13.smali"])
        self.assertIn("only uniform grids", printed.call_args.args[0])

    def test_amounts_must_fit_the_instructions(self):
        for kwargs in ({"rows": 8}, {"rows": 1}, {"screens": 1.0}, {"screens": 9.0}):
            with self.subTest(**kwargs), self.assertRaises(ValueError):
                patch_iisu.widen_browser_prefetch(Path("."), **kwargs)
        with self.assertRaises(ValueError):
            patch_iisu._float_bits(2.1)


class RaiseImageMemoryCacheTests(unittest.TestCase):
    """iiSU's Coil memory cache: maxSizePercent(0.16, context) in newImageLoader."""

    APPLICATION = (
        ".class public final Lcom/iisulauncher/LairLauncherApplication;\n"
        ".super Landroid/app/Application;\n\n"
        ".method public final b(Landroid/content/Context;)Lsx4;\n"
        "    new-instance v0, Lsy4;\n\n"
        "    const/16 v1, 0xa\n\n"
        "    invoke-direct {v0, v1}, Lsy4;-><init>(I)V\n\n"
        "    .line 12\n"
        "    const-wide v1, 0x3fc47ae147ae147bL    # 0.16\n\n"
        "    invoke-virtual {v0, v1, v2, p0}, Lsy4;->m(DLandroid/content/Context;)V\n\n"
        "    const-wide v3, 0x3fc47ae147ae147bL    # 0.16\n"
        ".end method\n"
    )

    def _run(self, text: str, **kwargs):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "smali" / "com" / "iisulauncher" / "LairLauncherApplication.smali"
            path.parent.mkdir(parents=True)
            path.write_text(text, encoding="utf-8")
            with patch("builtins.print") as printed:
                changed = patch_iisu.raise_image_memory_cache(Path(tmp), **kwargs)
            return changed, path.read_text(encoding="utf-8"), printed

    def test_the_share_passed_to_the_cache_builder_is_raised(self):
        changed, text, printed = self._run(self.APPLICATION)
        self.assertTrue(changed)
        self.assertIn("const-wide v1, 0x3fe0000000000000L", text)
        # an unrelated 0.16 in the same class is untouched
        self.assertIn("const-wide v3, 0x3fc47ae147ae147bL", text)
        printed.assert_not_called()

    def test_running_it_again_changes_nothing_and_says_nothing(self):
        _, once, _ = self._run(self.APPLICATION)
        changed, twice, printed = self._run(once)
        self.assertFalse(changed)
        self.assertEqual(once, twice)
        printed.assert_not_called()

    def test_a_different_share_in_a_newer_iisu_is_left_alone(self):
        newer = self.APPLICATION.replace("const-wide v1, 0x3fc47ae147ae147bL", "const-wide v1, 0x3fc999999999999aL", 1)
        changed, text, printed = self._run(newer)
        self.assertFalse(changed)
        self.assertEqual(text, newer)
        self.assertIn("isn't where expected", printed.call_args.args[0])

    def test_the_share_must_be_sensible(self):
        for share in (0.16, 0.8):
            with self.subTest(share=share), self.assertRaises(ValueError):
                patch_iisu.raise_image_memory_cache(Path("."), share=share)
