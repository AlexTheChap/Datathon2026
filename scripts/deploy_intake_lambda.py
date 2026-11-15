"""Déploie l'Intake (boîte ③) comme fonction Lambda, avec boto3 seulement (pas besoin de SAM ni de Docker).

    python scripts/deploy_intake_lambda.py --profile workshop            # crée ou met à jour la fonction
    python scripts/deploy_intake_lambda.py --profile workshop --test     # + invoque avec la démo de Robert

Ce que le script fait :
1. construit build/intake_lambda.zip : backend/app + prompt + assets_universe.csv + pydantic et anthropic
   (roues Linux x86_64 pour Python 3.12 ; boto3/botocore sont déjà dans le runtime Lambda) ;
2. crée le rôle IAM « alphaforge-intake-lambda » (journaux CloudWatch + bedrock:InvokeModel), sauf si --role-arn ;
3. crée ou met à jour la fonction « alphaforge-intake » (Python 3.12, AF_INTAKE_MODE=bedrock).

Aucune URL publique n'est créée : on l'appelle avec boto3 (lambda.invoke) ou depuis la future API ②.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
FUNCTION = "alphaforge-intake"
ROLE = "alphaforge-intake-lambda"
RUNTIME = "python3.12"
DEPS = ["pydantic>=2.7", "anthropic>=2.0"]

TRUST = {"Version": "2012-10-17", "Statement": [
    {"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
BEDROCK_POLICY = {"Version": "2012-10-17", "Statement": [{
    "Effect": "Allow",
    "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
    # Profil d'inférence « us. » + modèle de fondation dans chaque région US vers laquelle il route.
    "Resource": ["arn:aws:bedrock:*:*:inference-profile/us.anthropic.claude-haiku-5-5",
                 "arn:aws:bedrock:*::foundation-model/anthropic.claude-haiku-5-5"],
}]}


def build_zip() -> Path:
    pkg = BUILD / "intake_pkg"
    shutil.rmtree(pkg, ignore_errors=True)
    pkg.mkdir(parents=True)
    print("• Installation des dépendances (roues Linux, Python 3.12)…")
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--target", str(pkg),
                    "--platform", "manylinux2014_x86_64", "--implementation", "cp", "--python-version", "3.12",
                    "--only-binary=:all:", *DEPS], check=True)
    shutil.copytree(ROOT / "backend" / "app", pkg / "app",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".gitkeep"))
    (pkg / "data").mkdir()
    shutil.copy(ROOT / "data" / "curated" / "assets_universe.csv", pkg / "data" / "assets_universe.csv")

    zip_path = BUILD / "intake_lambda.zip"
    zip_path.unlink(missing_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in pkg.rglob("*"):
            if f.is_file() and "__pycache__" not in f.parts:
                z.write(f, f.relative_to(pkg).as_posix())
    print(f"• Paquet : {zip_path} ({zip_path.stat().st_size / 1e6:.1f} Mo)")
    return zip_path


def ensure_role(iam) -> str:
    try:
        arn = iam.get_role(RoleName=ROLE)["Role"]["Arn"]
        print(f"• Rôle existant : {arn}")
    except iam.exceptions.NoSuchEntityException:
        arn = iam.create_role(RoleName=ROLE, AssumeRolePolicyDocument=json.dumps(TRUST),
                              Description="AlphaForge Intake Lambda (boîte 3)")["Role"]["Arn"]
        print(f"• Rôle créé : {arn}")
        time.sleep(10)  # propagation IAM avant que Lambda puisse l'assumer
    iam.attach_role_policy(RoleName=ROLE,
                           PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole")
    iam.put_role_policy(RoleName=ROLE, PolicyName="bedrock-invoke-haiku", PolicyDocument=json.dumps(BEDROCK_POLICY))
    return arn


def deploy(lam, zip_path: Path, role_arn: str, region: str) -> None:
    code = zip_path.read_bytes()
    env = {"Variables": {"AF_INTAKE_MODE": "bedrock", "AF_UNIVERSE_CSV": "/var/task/data/assets_universe.csv"}}
    try:
        lam.get_function(FunctionName=FUNCTION)
        lam.update_function_code(FunctionName=FUNCTION, ZipFile=code)
        lam.get_waiter("function_updated_v2").wait(FunctionName=FUNCTION)
        lam.update_function_configuration(FunctionName=FUNCTION, Environment=env, Timeout=60, MemorySize=512)
        print(f"• Fonction mise à jour : {FUNCTION}")
    except lam.exceptions.ResourceNotFoundException:
        for attempt in range(6):  # le rôle tout neuf peut mettre quelques secondes à être assumable
            try:
                lam.create_function(FunctionName=FUNCTION, Runtime=RUNTIME, Role=role_arn,
                                    Handler="app.api.intake_handler.handler", Code={"ZipFile": code},
                                    Timeout=60, MemorySize=512, Environment=env,
                                    Description="AlphaForge Intake (boîte 3) : texte libre → Profile + Request JSON")
                break
            except lam.exceptions.InvalidParameterValueException as e:
                if "role" not in str(e).lower() or attempt == 5:
                    raise
                time.sleep(5)
        print(f"• Fonction créée : {FUNCTION}")
    lam.get_waiter("function_updated_v2").wait(FunctionName=FUNCTION)
    print(f"• Prête dans {region}.")


def test(lam) -> None:
    event = {"actor_id": "robert", "text": "Je suis prudent, horizon 3 à 5 ans, expérience avancée, 400 000 $, "
                                           "pas de crypto. Je pense mettre 20 % dans Nvidia, tout le monde en parle."}
    out = lam.invoke(FunctionName=FUNCTION, Payload=json.dumps(event).encode())
    payload = json.loads(out["Payload"].read())
    if out.get("FunctionError"):
        print("✗ Erreur Lambda :", json.dumps(payload, ensure_ascii=False, indent=2))
        sys.exit(1)
    print(f"• Test (HTTP {payload['statusCode']}) :")
    print(json.dumps(json.loads(payload["body"]), ensure_ascii=False, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", help="profil AWS (ex. workshop) ; sinon variables d'environnement AWS_*")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--role-arn", help="rôle IAM existant, si le compte interdit d'en créer")
    ap.add_argument("--test", action="store_true", help="invoquer la fonction après le déploiement")
    ap.add_argument("--test-only", action="store_true", help="seulement invoquer la fonction déjà déployée")
    args = ap.parse_args()

    try:  # Windows : certificats du système pour botocore
        import truststore
        truststore.inject_into_ssl()
    except ImportError:
        pass
    import boto3

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    print(f"• Compte : {session.client('sts').get_caller_identity()['Account']}  région : {args.region}")
    lam = session.client("lambda")
    if not args.test_only:
        zip_path = build_zip()
        role_arn = args.role_arn or ensure_role(session.client("iam"))
        deploy(lam, zip_path, role_arn, args.region)
    if args.test or args.test_only:
        test(lam)


if __name__ == "__main__":
    main()
