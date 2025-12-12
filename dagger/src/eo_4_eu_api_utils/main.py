import dagger
import tomllib
import requests
from pathlib import Path
from typing import Annotated
from build import ProjectBuilder
from twine.settings import Settings
from twine.commands.upload import upload
from distlib.locators import PyPIJSONLocator
from dagger import Doc, dag, function, object_type, File, Directory


@object_type
class Eo4euApiUtils:
    @function
    async def build(
        self,
        password: Annotated[dagger.Secret, Doc("The PyPI token")],
        regurl: Annotated[str, Doc("The URL of the package registry")],
        uploadurl: Annotated[str, Doc("The URL to use for uploads")],
        project: Annotated[str, Doc("The name of the project")],
        wkd: Annotated[
            dagger.Directory,
            Doc("Location of directory containing Dagger files"),
        ],
    ):
        dirname = "_build"
        token = await password.plaintext()
        await wkd.export(dirname)

        root = Path(dirname)
        proj_file = root.joinpath("pyproject.toml")
        dist_dir = root.joinpath("dist")

        version = tomllib.load(proj_file.open("rb"))["project"]["version"]
        print(f"Found version: {version}")

        locator = PyPIJSONLocator(regurl)
        release = locator.locate(f"{project}=={version}", prereleases = True)
        if release is not None:
            print(f"{project}=={version} already exists, skipping build...")
            return

        builder = ProjectBuilder(root)
        wheel_path = builder.build("wheel", dist_dir)
        sdist_path = builder.build("sdist", dist_dir)

        upload(
            Settings(
                password = token,
                repository_url = uploadurl
            ),
            dists = [wheel_path, sdist_path]
        )

    @function
    async def docs(
        self,
        password: Annotated[dagger.Secret, Doc("The gitlab password")],
        regurl: Annotated[str, Doc("The URL for the docs package registry")],
        docdir: Annotated[str, Doc("The docs subdirectory name")],
        package: Annotated[str, Doc("The name of the project\'s doc package")],
        wkd: Annotated[
            dagger.Directory,
            Doc("Location of directory containing Dagger files"),
        ],
    ):
        token = await password.plaintext()
        proj_file_str = await wkd.file("pyproject.toml").contents()
        docs_file_str = await wkd.file(f"{docdir}/VERSION.txt").contents()

        proj_version = tomllib.loads(proj_file_str)["project"]["version"]
        docs_version = f"{proj_version}.v{docs_file_str.strip()}"
        print(f"Found docs version: {docs_version}")

        response = requests.get(regurl, headers = {"PRIVATE-TOKEN": token}).json()
        for item in response:
            if item["name"] == package and item["version"] == docs_version:
                print(f"{package}=={docs_version} already exists, skipping build...")
                return

        out_archive = f"{package}-{docs_version}.zip"
        await (
            dag.container(platform=dagger.Platform("linux/amd64"))
            .from_("python:3.12-alpine")
            .with_directory("/workdir", wkd)
            .with_workdir(f"/workdir/{docdir}")
            .with_exec(["apk", "add", "zip"])
            .with_exec([
                "python3", "-m", "pip", "install", "-e", "..[docs]",
            ])
            .with_exec([
                "python3", "-m", "sphinx", "build", "source", "build",
            ])
            .with_workdir(f"/workdir/{docdir}/build")
            .with_exec(["zip", "-r", "/tmp/docs.zip", "."])
            .file("/tmp/docs.zip")
            .export(out_archive)
        )

        print("Uploading...")
        upload_response = requests.put(
            url = f"{regurl}/generic/{package}/{docs_version}/{out_archive}",
            headers = {"PRIVATE-TOKEN": token},
            files = {"file": Path(out_archive).open("rb")}
        )
        if upload_response.status_code >= 400:
            raise ConnectionError(
                f"Failed to upload docs: HTTP {upload_response.status_code} - "
                f"{upload_response.text}"
            )
        
    @function
    async def analyze_with_sonarqube(
        self,
        yaml_rules: Annotated[File, Doc("YAML file containing security rules extracted from PDF")],
        source_directory: Annotated[Directory, Doc("Source code directory to analyze")],
        sonar_host_url: Annotated[str, Doc("SonarQube server URL")],
        sonar_token: Annotated[str, Doc("SonarQube authentication token")],
        sonar_project_key: Annotated[str, Doc("SonarQube project key")],
        output_name: Annotated[str, Doc("Name for the SARIF output file")] = "sonarqube-results.sarif",
        create_quality_profile: Annotated[bool, Doc("Whether to create a custom Quality Profile based on PDF rules")] = False,
    ) -> Annotated[Directory, Doc("Directory containing SARIF JSON report and HTML report")]:
        """
        Run SonarQube compliance analysis based on PDF-extracted security rules.
        
        This function:
        1. Loads PDF security rules from YAML
        2. Optionally creates a custom SonarQube Quality Profile with only those rules
        3. Runs sonar-scanner using the custom profile (if created) or default rules
        4. Fetches analysis results from SonarQube API
        5. Generates SARIF 2.1.0 format output (JSON)
        6. Converts SARIF to HTML using sarif-tools
        
        The analysis uses rule_mapping.yaml to map PDF rules (e.g., OBJ01-J) 
        to SonarQube rules (e.g., java:S1104). This ensures compliance checking
        is based on the specific rules extracted from the PDF document.
        
        Quality Profile creation (--create-quality-profile=true):
        - Only supported for: c, cpp, java, python
        - Requires token with 'Administer Quality Profiles' permission
        - If disabled or fails, uses SonarQube default rules
        
        Requires:
        - SonarQube server to be accessible
        - Valid authentication token
        - rule_mapping.yaml with PDF->SonarQube rule mappings (if using Quality Profiles)
        
        Returns:
        - Directory with both .sarif (JSON) and .html files
        """
        
        module_source = dag.current_module().source()
        scanner_script = module_source.file("sonarqube_scanner.py")
        rule_mapping = module_source.file("rule_mapping.yaml")
        
        container = (
            dag.container()
            .from_("sonarsource/sonar-scanner-cli:latest")
            .with_user("root")
            .with_exec(["sh", "-c", "dnf install -y python3-pip"])
            .with_exec(["pip3", "install", "--no-cache-dir", "pyyaml", "requests", "sarif-tools"])
            .with_mounted_file("/workspace/rules.yaml", yaml_rules)
            .with_mounted_file("/workspace/rule_mapping.yaml", rule_mapping)
            .with_mounted_directory("/src", source_directory)
            .with_mounted_file("/workspace/sonarqube_scanner.py", scanner_script)
            .with_exec(["chown", "-R", "scanner-cli:scanner-cli", "/src"])
            .with_exec(["chmod", "-R", "u+w", "/src"])
            .with_exec(["mkdir", "-p", "/output"])
            .with_exec(["chown", "-R", "scanner-cli:scanner-cli", "/output"])
            .with_exec(["chown", "-R", "scanner-cli:scanner-cli", "/workspace"])
            .with_exec(["mkdir", "-p", "/src/target/classes"])
            .with_exec(["chown", "-R", "scanner-cli:scanner-cli", "/src/target"])
            .with_user("scanner-cli")
            .with_workdir("/src")
            .with_env_variable("SONAR_HOST_URL", sonar_host_url)
            .with_env_variable("SONAR_TOKEN", sonar_token)
            .with_env_variable("SONAR_PROJECT_KEY", sonar_project_key)
            .with_env_variable("CREATE_QUALITY_PROFILE", "true" if create_quality_profile else "false")
            .with_env_variable("PYTHONUNBUFFERED", "1")
            .with_exec([
                "python3", "/workspace/sonarqube_scanner.py",
                "/src",
                "/workspace/rules.yaml",
                f"/output/{output_name}"
            ])
            .with_exec([
                "sarif", "html",
                f"/output/{output_name}",
                "-o", f"/output/{output_name.replace('.sarif', '.html')}"
            ])
        )
        
        return container.directory("/output")

    @function
    async def analyze_with_gitguardian(
        self,
        gitguardian_api_key: Annotated[dagger.Secret, Doc("GitGuardian API Key")],
        source_directory: Annotated[Directory, Doc("Source code directory to analyze")],
        output_name: Annotated[str, Doc("Name for the SARIF output file")] = "gitguardian-results.sarif",
    ) -> Annotated[Directory, Doc("Directory containing SARIF JSON report and HTML report")]:
        """
        Run GitGuardian compliance analysis.
        """
        container = (
            dag.container()
            .from_("gitguardian/ggshield:latest")
            .with_user("root")
            .with_exec(["sh", "-c", "apt-get update && apt-get install -y python3-pip"])
            .with_exec(["pip3", "install", "--no-cache-dir", "pyyaml", "requests", "sarif-tools"])
            .with_mounted_directory("/src", source_directory)
            .with_workdir("/src")
            .with_directory("/output", dag.directory())
            .with_secret_variable("GITGUARDIAN_API_KEY", gitguardian_api_key)
        )
        py_run_ggshield = r"""
import os, sys, subprocess, shlex
out_path = "/output/{output}"
exclude_args = []
gitdir = ".git"
if os.path.isdir(gitdir):
    for root, _, files in os.walk(gitdir):
        for fn in files:
            rel = os.path.join(root, fn)
            exclude_args += ["--exclude", rel]
cmd = ["ggshield", "secret", "scan", "path", "--format", "sarif", "-r", "-y", "."] + exclude_args
env = os.environ.copy()
proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
if proc.returncode not in (0,):
    sys.stderr.buffer.write(proc.stderr)
with open(out_path, "wb") as f:
    f.write(proc.stdout)
print("WROTE SARIF ->", out_path)
""".format(output=output_name)
    
        container = (
            container
            .with_exec([
                "python3", "-c", py_run_ggshield
            ])
            .with_exec([
                "sarif", "html",
                f"/output/{output_name}",
                "-o", f"/output/{output_name.replace('.sarif', '.html')}"
            ])
        )
    
        return container.directory("/output")
    
    
    @function
    async def synthetic_report(
        self,
        sonar_sarif: Annotated[File, Doc("Sonar SARIF file, e.g. sonar-report.sarif")],
        gg_sarif: Annotated[File | None, Doc("GitGuardian SARIF file, e.g. gitguardian-report.sarif")] = None,
        sbom_file: Annotated[File | None, Doc("CycloneDX SBOM JSON file, e.g. sbom-report.cdx.json")] = None,
        severity_threshold: Annotated[str, Doc("Minimum vulnerability severity to include (CRITICAL/HIGH/MEDIUM/LOW/INFO)")] = "HIGH",
    ) -> Annotated[Directory, Doc("Directory containing synthetic report HTML and JSON summary")]:
        """
        Combine SBOM CycloneDX JSON, Sonar SARIF, and optional GitGuardian SARIF,
        filter vulnerabilities above `severity_threshold` and error-level issues,
        and produce an HTML report plus a JSON summary. Returns a directory with
        `/output/synthetic-report.html` and `/output/synthetic-report.json`.
        """
        module_source = dag.current_module().source()
        report_script = module_source.file("generate_report.py")
    
        container = dag.container().from_("python:3.11-slim")
    
        if sbom_file is not None:
            container = container.with_mounted_file("/input/sbom.json", sbom_file)
    
        container = container.with_mounted_file("/input/sonar.sarif", sonar_sarif)
    
        if gg_sarif is not None:
            container = container.with_mounted_file("/input/gitguardian.sarif", gg_sarif)
    
        container = container.with_mounted_file("/workspace/generate_report.py", report_script)
        container = container.with_exec(["mkdir", "-p", "/output"])
        container = container.with_env_variable("THRESHOLD", severity_threshold.upper())
    
        cmd = ["python3", "/workspace/generate_report.py"]
        if sbom_file is not None:
            cmd += ["--sbom", "/input/sbom.json"]
        cmd += ["--sonar-sarif", "/input/sonar.sarif"]
        if gg_sarif is not None:
            cmd += ["--gg-sarif", "/input/gitguardian.sarif"]
        cmd += ["--threshold", severity_threshold.upper(), "--outdir", "/output"]
    
        container = container.with_exec(cmd)
    
        return container.directory("/output")


