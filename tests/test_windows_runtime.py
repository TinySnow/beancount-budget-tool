"""Only Windows onefile embeds a user-owned bootloader extraction base."""

import os
from pathlib import Path
import runpy
import sys
from types import ModuleType,SimpleNamespace
import unittest
from unittest.mock import Mock,patch


class RuntimePathTests(unittest.TestCase):
    def build(self,platform,mode):
        root=Path(__file__).resolve().parents[1]
        package=ModuleType('PyInstaller');utilities=ModuleType('PyInstaller.utils');hooks=ModuleType('PyInstaller.utils.hooks')
        hooks.collect_data_files=lambda *a,**k:[]
        hooks.collect_submodules=lambda *a,**k:[]
        hooks.copy_metadata=lambda *a,**k:[]
        exe=Mock()
        globals={'SPECPATH':str(root/'packaging'),'Analysis':lambda *a,**k:SimpleNamespace(pure=[],scripts=[],binaries=[],datas=[]),
                 'PYZ':lambda *a,**k:object(),'EXE':exe,'COLLECT':Mock()}
        with patch.dict(sys.modules,{'PyInstaller':package,'PyInstaller.utils':utilities,'PyInstaller.utils.hooks':hooks}),\
             patch.object(sys,'platform',platform),patch.dict(os.environ,{'BUDGET_BUNDLE_MODE':mode}):
            runpy.run_path(str(root/'packaging/windows.spec'),init_globals=globals)
        return exe.call_args.kwargs

    def test_windows_onefile_uses_private_local_appdata_path(self):
        args=self.build('win32','onefile')
        self.assertEqual(args['runtime_tmpdir'],r'%LOCALAPPDATA%\BeancountBudgetTool\Runtime')
        self.assertFalse(args['exclude_binaries'])

    def test_windows_folder_mode_does_not_need_an_extraction_path(self):
        args=self.build('win32','onedir')
        self.assertIsNone(args['runtime_tmpdir'])
        self.assertTrue(args['exclude_binaries'])

    def test_mac_onefile_still_uses_its_native_temporary_directory(self):
        self.assertIsNone(self.build('darwin','onefile')['runtime_tmpdir'])


if __name__=='__main__':unittest.main()
