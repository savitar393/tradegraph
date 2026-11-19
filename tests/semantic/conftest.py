"""Run Java contracts through Maven and read fresh Surefire results."""

from collections import Counter
from pathlib import Path
import shutil
import subprocess
from xml.etree import ElementTree

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def maven_contract(tmp_path_factory):
    def run(class_name):
        maven = shutil.which("mvn")
        assert maven, "Put Maven on PATH before running the semantic contract."
        reports = tmp_path_factory.mktemp("semantic-reports")
        result = subprocess.run(
            [
                maven,
                "-B",
                "-ntp",
                "-f",
                "semantic/pom.xml",
                f"-Dtest={class_name}",
                f"-Dcontract.reportsDirectory={reports}",
                "test",
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            timeout=180,
            check=False,
        )
        assert result.returncode == 0, result.stdout
        report = reports / f"TEST-id.tradegraph.{class_name}.xml"
        assert report.is_file(), f"No fresh {class_name} report.\n{result.stdout}"
        counts = Counter()
        for case in ElementTree.parse(report).getroot().findall("testcase"):
            for status in ("failure", "error", "skipped"):
                assert case.find(status) is None, ElementTree.tostring(case, encoding="unicode")
            method_name = case.attrib["name"].split("(")[0].split("[")[0]
            counts[method_name] += 1
        return counts

    return run
