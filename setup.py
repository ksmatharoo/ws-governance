"""Classic setuptools packaging for databricks-governance.

Kept setup.py-based (rather than pyproject-only) so legacy / Windows CI agents can build
the wheel with the conventional invocation:

    python setup.py bdist_wheel

All dependencies live in requirements.txt (single source of truth) and are read in below
as install_requires.
"""

from pathlib import Path

from setuptools import find_packages, setup

HERE = Path(__file__).parent.resolve()


def _read_requirements(filename):
    """Return non-comment, non-empty requirement lines from the given file."""
    reqs = []
    for raw in (HERE / filename).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            reqs.append(line)
    return reqs


def _read_long_description():
    readme = HERE / "README.md"
    return readme.read_text(encoding="utf-8") if readme.exists() else ""


setup(
    name="databricks-governance",
    version="0.1.0",
    description=(
        "Config-driven Databricks governance framework "
        "(workspace object ACLs + Unity Catalog)"
    ),
    long_description=_read_long_description(),
    long_description_content_type="text/markdown",
    python_requires=">=3.9",
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    # Single source of truth: every dependency lives in requirements.txt.
    install_requires=_read_requirements("requirements.txt"),
    entry_points={
        "console_scripts": [
            "governance=governance.cli:main",
        ],
    },
    include_package_data=True,
    zip_safe=False,
)
