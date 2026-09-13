"""Capture AWS console screenshots of the deployed CostCare stack.

Signs in with a temporary, READ-ONLY federated console session created from your AWS CLI
credentials (sts:GetFederationToken, 1 hour). The session policy only allows viewing the
services CostCare uses and cannot read secret values. No password is typed, and the sign-in
URL is never printed or written to disk.

  python deploy/console_screenshots.py --stack costcare-prod --region af-south-1
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote, quote_plus

import httpx
from playwright.sync_api import Page, sync_playwright

OUT = Path(__file__).resolve().parent.parent / "docs" / "screenshots"

READ_ONLY_SESSION_POLICY = {
    "Version": "2012-10-17",
    "Statement": [{
        "Sid": "ViewCostCareResourcesOnly",
        "Effect": "Allow",
        "Action": [
            "cloudformation:Describe*", "cloudformation:List*", "cloudformation:Get*",
            "lambda:Get*", "lambda:List*",
            "apigateway:GET",
            "cloudwatch:Describe*", "cloudwatch:Get*", "cloudwatch:List*",
            "logs:Describe*", "logs:Get*", "logs:FilterLogEvents", "logs:StartQuery", "logs:StopQuery",
            "logs:GetQueryResults",
            "s3:ListAllMyBuckets", "s3:ListBucket", "s3:GetBucket*", "s3:GetEncryptionConfiguration",
            "s3:GetLifecycleConfiguration", "s3:GetAccountPublicAccessBlock",
            "secretsmanager:ListSecrets", "secretsmanager:DescribeSecret",
            "events:Describe*", "events:List*",
            "xray:Get*", "xray:BatchGet*", "xray:List*",
            "sns:List*", "sns:Get*",
            "tag:GetResources", "resource-groups:List*", "resource-explorer-2:List*",
        ],
        "Resource": "*",
    }],
}

# Height of the dark AWS console navigation bar, which displays the account ID
HEADER_HEIGHT = 52

# Cookie banner (we decline non-essential cookies) and first-visit console tours
POPUP_BUTTONS = re.compile(r"^(Decline|Next|Done|Finish|Got it|Dismiss|Skip|End tour)$", re.I)


def aws(*args: str) -> dict:
    result = subprocess.run(["aws", *args, "--output", "json"], capture_output=True, text=True,
                            shell=sys.platform == "win32")
    if result.returncode != 0:
        raise SystemExit(f"aws {' '.join(args[:2])} failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def console_login_url(destination: str) -> str:
    creds = aws("sts", "get-federation-token", "--name", "costcare-screenshots", "--duration-seconds", "3600",
                "--policy", json.dumps(READ_ONLY_SESSION_POLICY))["Credentials"]
    session = json.dumps({"sessionId": creds["AccessKeyId"], "sessionKey": creds["SecretAccessKey"],
                          "sessionToken": creds["SessionToken"]})
    token = httpx.get("https://signin.aws.amazon.com/federation",
                      params={"Action": "getSigninToken", "Session": session}, timeout=30).json()["SigninToken"]
    return ("https://signin.aws.amazon.com/federation?Action=login&Issuer=costcare-screenshots"
            f"&Destination={quote_plus(destination)}&SigninToken={token}")


def dismiss_popups(page: Page) -> None:
    for _ in range(8):
        buttons = page.get_by_role("button", name=POPUP_BUTTONS)
        clicked = False
        for i in range(buttons.count()):
            button = buttons.nth(i)
            try:
                if button.is_visible():
                    button.click(timeout=2000)
                    page.wait_for_timeout(700)
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            return


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stack", default="costcare-prod")
    parser.add_argument("--region", default="af-south-1")
    parser.add_argument("--channel", default="msedge")
    parser.add_argument("--only", nargs="*", help="Capture only these file names")
    args = parser.parse_args()
    region, stack_name = args.region, args.stack

    stack = aws("cloudformation", "describe-stacks", "--stack-name", stack_name, "--region", region)["Stacks"][0]
    outputs = {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}
    api_id = outputs["WebsiteUrl"].split("//", 1)[1].split(".", 1)[0]
    web_fn, sync_fn = outputs["WebFunctionName"], outputs["GoogleSyncFunctionName"]
    bucket = outputs["UploadsBucketName"]
    stage = stack_name.removeprefix("costcare-")
    console = f"https://{region}.console.aws.amazon.com"
    account = stack["StackId"].split(":")[4]
    account_pattern = re.compile(rf"{account}|{account[:4]}-{account[4:8]}-{account[8:]}")

    # (file name, url, seconds to settle, viewport height, scroll pixels)
    pages = [
        ("aws-01-cloudformation-resources.png",
         f"{console}/cloudformation/home?region={region}#/stacks/resources?stackId={quote(stack['StackId'], safe='')}",
         10, 1000, 0),
        ("aws-02-lambda-web-function.png", f"{console}/lambda/home?region={region}#/functions/{web_fn}?tab=code", 12, 1000, 0),
        ("aws-03-lambda-monitoring.png", f"{console}/lambda/home?region={region}#/functions/{web_fn}?tab=monitoring",
         16, 1000, 620),
        ("aws-04-api-gateway.png", f"{console}/apigateway/main/api-detail?api={api_id}&region={region}", 10, 1000, 0),
        ("aws-05-cloudwatch-dashboard.png",
         f"{console}/cloudwatch/home?region={region}#dashboards/dashboard/CostCare-{stage}?start=PT1H", 18, 1500, 0),
        ("aws-06-cloudwatch-alarms.png", f"{console}/cloudwatch/home?region={region}#alarmsV2:?search=costcare", 10, 1000, 0),
        ("aws-07-eventbridge-schedule.png",
         f"{console}/events/home?region={region}#/eventbus/default/rules/costcare-{stage}-daily-google-sync", 10, 1000, 0),
        ("aws-08-lambda-google-sync.png", f"{console}/lambda/home?region={region}#/functions/{sync_fn}?tab=configure",
         12, 1000, 0),
        ("aws-09-s3-uploads-bucket.png",
         f"https://s3.console.aws.amazon.com/s3/buckets/{bucket}?region={region}&tab=properties", 10, 1000, 0),
        ("aws-10-secrets-manager.png", f"{console}/secretsmanager/listsecrets?region={region}&search=costcare", 10, 1000, 0),
    ]
    if args.only:
        pages = [p for p in pages if p[0] in set(args.only)]

    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel=args.channel, headless=True)
        context = browser.new_context(viewport={"width": 1600, "height": 1000}, locale="en-US")
        page = context.new_page()
        page.goto(console_login_url(f"{console}/console/home?region={region}"), wait_until="load", timeout=90000)
        page.wait_for_timeout(8000)
        dismiss_popups(page)
        for name, url, settle_seconds, height, scroll in pages:
            try:
                page.set_viewport_size({"width": 1600, "height": height})
                page.goto(url, wait_until="load", timeout=90000)
                page.wait_for_timeout(settle_seconds * 1000)
                dismiss_popups(page)
                if scroll:
                    page.mouse.move(700, 700)
                    page.mouse.wheel(0, scroll)
                    page.wait_for_timeout(4000)
                # Crop off the console header (shows the account ID) and cover any ARN text containing it
                page.screenshot(
                    path=OUT / name,
                    clip={"x": 0, "y": HEADER_HEIGHT, "width": 1600, "height": height - HEADER_HEIGHT},
                    mask=[page.get_by_text(account_pattern)],
                    mask_color="#d5dbdb",
                )
                print(f"saved {OUT / name}")
            except Exception as exc:  # keep capturing the other pages
                print(f"skipped {name}: {exc}")
        browser.close()


if __name__ == "__main__":
    main()
