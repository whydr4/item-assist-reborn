import contextlib
import importlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline'))
pack = importlib.import_module('pack')
queries = importlib.import_module('queries')

class PipelineRegression(unittest.TestCase):
    def test_snapshot_bound_is_part_of_every_query_filter(self):
        self.assertEqual(queries.Q(100, 200)._f('all')['max_unix_timestamp'], 200)

    def run_pack(self, root, successful):
        cs2 = root / 'cs2'
        binary = cs2 / 'game/bin/win64'
        binary.mkdir(parents=True)
        (binary / 'resourcecompiler.exe').touch()
        (binary / 'tier0.dll').touch()
        (root / 'source.js').write_text('var x=1;')
        generated = cs2 / 'game/csgo_addons/item_assist_reborn/panorama/scripts/source.vjs_c'
        generated.parent.mkdir(parents=True)
        generated.write_bytes(b'stale')
        def compile_script(args, **kwargs):
            self.assertFalse(generated.exists(), 'stale asset must be removed before compiling')
            self.assertEqual(args[0], 'wine')
            self.assertIn('Z:', args[args.index('-i') + 1])
            if successful:
                generated.write_bytes(b'fresh')
            return SimpleNamespace(returncode=1, stdout='OK: 1 compiled, 0 failed' if successful else 'FAILED', stderr='')
        with patch.multiple(pack, ROOT=str(root), PREBUILT=str(root/'prebuilt'), SOURCES={'source.js':'panorama/scripts/source.js'}), \
             patch.object(sys, 'argv', ['pack.py','--cs2',str(cs2),'--install','--addons',str(root/'addons'),'--wine-prefix',str(root/'wine')]), \
             patch.object(pack.shutil,'which',return_value='/usr/bin/wine'), \
             patch.object(pack.subprocess,'run',side_effect=compile_script), contextlib.redirect_stdout(io.StringIO()):
            pack.main()
        return root/'addons/pak30_dir.vpk'

    @unittest.skipIf(sys.platform == 'win32', 'Linux Wine execution test')
    def test_wine_success_requires_fresh_output_and_installs_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            installed=self.run_pack(root,True)
            self.assertEqual(pack.vpk.read_file(str(installed), pack.vpk.read_entries(str(installed))['panorama/scripts/source.vjs_c']),b'fresh')

    @unittest.skipIf(sys.platform == 'win32', 'Linux Wine execution test')
    def test_failed_compile_cannot_reuse_stale_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with self.assertRaises(SystemExit):
                self.run_pack(root,False)
            self.assertFalse((root/'addons/pak30_dir.vpk').exists())


class MetadataRegression(unittest.TestCase):
    def test_current_assets_replace_old_names_costs_and_components(self):
        build = importlib.import_module('build')
        assets = [
            {'id':1,'class_name':'upgrade_base','name':'Base','type':'upgrade','shopable':True,'cost':800,'item_tier':1},
            {'id':2,'class_name':'upgrade_new','name':'New name','type':'upgrade','shopable':True,'cost':3200,'item_tier':3,'component_items':['upgrade_base']},
            {'id':3,'class_name':'upgrade_removed','name':'Removed','type':'upgrade','shopable':False},
        ]
        old={'items':{'2':{'n':'Old name','c':999,'t':2,'comp':[99],'ic':'icon','s':'spirit'}}}
        items,shop=build.build_item_metadata(assets,old,[])
        self.assertEqual(items['2']['n'],'New name')
        self.assertEqual(items['2']['c'],3200)
        self.assertEqual(items['2']['comp'],[1])
        self.assertNotIn('3',items)
        self.assertNotIn('comp',items['1'])

if __name__ == '__main__':
    unittest.main()
