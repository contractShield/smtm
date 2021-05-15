"""업비트 거래소를 통한 거래 처리"""

import os
import copy
import jwt  # PyJWT
import uuid
import hashlib
from urllib.parse import urlencode
import requests
import threading
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

from .log_manager import LogManager
from .trader import Trader
from .worker import Worker


class UpbitTrader(Trader):
    """
    거래 요청 정보를 받아서 거래소에 요청하고 거래소에서 받은 결과를 제공해주는 클래스

    id: 요청 정보 id "1607862457.560075"
    type: 거래 유형 sell, buy, cancel
    price: 거래 가격
    amount: 거래 수량
    """

    RESULT_CHECKING_INTERVAL = 5
    MARKET = "KRW-BTC"
    MARKET_CURRENCY = "BTC"
    ISO_DATEFORMAT = "%Y-%m-%dT%H:%M:%S"
    COMMISSION_RATIO = 0.0005

    def __init__(self, budget=50000):
        self.logger = LogManager.get_logger(__class__.__name__)
        self.worker = Worker("UpbitTrader-Worker")
        self.worker.start()
        self.timer = None
        self.order_map = {}
        self.ACCESS_KEY = os.environ.get("UPBIT_OPEN_API_ACCESS_KEY", "upbit_access_key")
        self.SECRET_KEY = os.environ.get("UPBIT_OPEN_API_SECRET_KEY", "upbit_secret_key")
        self.SERVER_URL = os.environ.get("UPBIT_OPEN_API_SERVER_URL", "upbit_server_url")
        self.is_opt_mode = True
        self.asset = (0, 0)  # avr_price, amount
        self.balance = budget
        self.name = "Upbit"

    def send_request(self, requests, callback):
        """거래 요청을 처리한다

        requests: 한 개 이상의 거래 요청 정보 리스트
        [{
            "id": 요청 정보 id "1607862457.560075"
            "type": 거래 유형 sell, buy, cancel
            "price": 거래 가격
            "amount": 거래 수량
            "date_time": 요청 데이터 생성 시간
        }]
        callback(result):
        {
            "request": 요청 정보
            "type": 거래 유형 sell, buy, cancel
            "price": 거래 가격
            "amount": 거래 수량
            "state": 거래 상태 requested, done
            "msg": 거래 결과 메세지
            "date_time": 거래 체결 시간
        }
        """
        for request in requests:
            self.worker.post_task(
                {"runnable": self._execute_order, "request": request, "callback": callback}
            )

    def get_account_info(self):
        """계좌 요청 정보를 요청한다
        Returns:
            {
                balance: 계좌 현금 잔고
                asset: 자산 목록, 마켓이름을 키값으로 갖고 (평균 매입 가격, 수량)을 갖는 딕셔너리
                quote: 종목별 현재 가격 딕셔너리
                date_time: 현재 시간
            }
        """
        trade_info = self.get_trade_tick()
        result = {
            "balance": self.balance,
            "asset": {self.MARKET_CURRENCY: self.asset},
            "quote": {},
            "date_time": datetime.now().strftime(self.ISO_DATEFORMAT),
        }
        result["quote"][self.MARKET_CURRENCY] = float(trade_info[0]["trade_price"])
        self.logger.debug(f"account info {result}")
        return result

    def cancel_request(self, request_id):
        """거래 요청을 취소한다
        request_id: 취소하고자 하는 request의 id
        """
        if request_id not in self.order_map:
            return

        order = self.order_map[request_id]
        del self.order_map[request_id]
        result = order["result"]
        response = self._cancel_order(order["uuid"])

        if response is None:
            response = self._query_order_list([order["uuid"]], True)
            if len(response) > 0:
                response = response[0]
            else:
                return

        self.logger.debug(f"canceled order {response}")
        result["date_time"] = response["created_at"].replace("+09:00", "")
        # 최종 체결 가격, 수량으로 업데이트
        result["price"] = float(response["price"]) if response["price"] is not None else 0
        result["amount"] = float(response["executed_volume"])
        result["state"] = "done"
        self._call_callback(order["callback"], result)

    def cancel_all_requests(self):
        """모든 거래 요청을 취소한다
        체결되지 않고 대기중인 모든 거래 요청을 취소한다
        """
        orders = copy.deepcopy(self.order_map)
        for request_id, request_info in orders.items():
            self.cancel_request(request_id)

    def get_trade_tick(self):
        querystring = {"market": self.MARKET, "count": "1"}
        return self._request_get(self.SERVER_URL + "/v1/trades/ticks", params=querystring)

    def _execute_order(self, task):
        request = task["request"]
        if request["type"] == "cancel":
            self.cancel_request(request["id"])
            return

        if request["price"] == 0:
            self.logger.warning("invalid price request. market price is not supported now")
            return

        is_buy = True if request["type"] == "buy" else False
        if is_buy and float(request["price"]) * float(request["amount"]) > self.balance:
            self.logger.warning("invalid price request. balance is too small!")
            task["callback"]("error!")
            return

        if is_buy == False and float(request["amount"]) > self.asset[1]:
            self.logger.warning("invalid price request. rest asset amount is less than request!")
            task["callback"]("error!")
            return

        response = self._send_order(self.MARKET, is_buy, request["price"], request["amount"])
        if response is None:
            task["callback"]("error!")
            return

        result = self._create_success_result(request, response["uuid"])
        self.order_map[request["id"]] = {
            "uuid": response["uuid"],
            "callback": task["callback"],
            "result": result,
        }
        task["callback"](result)
        self.logger.debug(f"request inserted {self.order_map[request['id']]}")
        self._start_timer()

    def _create_success_result(self, request, uuid):
        return {
            "uuid": uuid,
            "state": "requested",
            "request": request,
            "type": request["type"],
            "price": request["price"],
            "amount": request["amount"],
            "msg": "success",
        }

    def _start_timer(self):
        if self.timer is not None:
            return

        def post_query_result_task():
            self.worker.post_task({"runnable": self._query_order_result})

        self.timer = threading.Timer(self.RESULT_CHECKING_INTERVAL, post_query_result_task)
        self.timer.start()

    def _stop_timer(self):
        if self.timer is None:
            return

        self.timer.cancel()
        self.timer = None

    def _query_order_result(self, task):
        uuids = []
        for request_id, request_info in self.order_map.items():
            uuids.append(request_info["uuid"])

        if len(uuids) == 0:
            return

        results = self._query_order_list(uuids, True)
        if results is None:
            return

        waiting_request = {}
        self.logger.debug(f"waiting order count {len(self.order_map)}")
        for request_id, request_info in self.order_map.items():
            is_done = False
            for order in results:
                if order["uuid"] == request_info["uuid"]:
                    self.logger.debug(f"Find done order! =====")
                    self.logger.debug(request_info)
                    self.logger.debug(order)
                    result = request_info["result"]
                    result["date_time"] = order["created_at"].replace("+09:00", "")
                    # 최종 체결 가격, 수량으로 업데이트
                    result["price"] = float(order["price"]) if order["price"] is not None else 0
                    result["amount"] = float(order["executed_volume"])
                    result["state"] = "done"
                    self._call_callback(request_info["callback"], result)
                    is_done = True

            if is_done is False:
                self.logger.debug(f"waiting order {request_info}")
                waiting_request[request_id] = request_info
        self.order_map = waiting_request
        self.logger.debug(f"After update, waiting order count {len(self.order_map)}")

        self._stop_timer()
        if len(self.order_map) > 0:
            self._start_timer()

    def _call_callback(self, callback, result):
        result_value = float(result["price"]) * float(result["amount"])
        fee = result_value * self.COMMISSION_RATIO

        if result["state"] == "done" and result["type"] == "buy":
            old_value = self.asset[0] * self.asset[1]
            new_value = old_value + result_value
            new_amount = self.asset[1] + float(result["amount"])
            new_amount = round(new_amount, 6)
            if new_amount == 0:
                avr_price = 0
            else:
                avr_price = new_value / new_amount
            self.asset = (avr_price, new_amount)
            self.balance -= round(result_value + fee)
        elif result["state"] == "done" and result["type"] == "sell":
            old_avr_price = self.asset[0]
            new_amount = self.asset[1] - float(result["amount"])
            new_amount = round(new_amount, 6)
            if new_amount == 0:
                old_avr_price = 0
            self.asset = (old_avr_price, new_amount)
            self.balance += round(result_value - fee)

        callback(result)

    def _send_order(self, market, is_buy, price=None, volume=None):
        """
        request:
            market *: 마켓 ID (필수)
            side *: 주문 종류 (필수)
            - bid : 매수
            - ask : 매도
            volume *: 주문량 (지정가, 시장가 매도 시 필수)
            price *: 주문 가격. (지정가, 시장가 매수 시 필수)
            ex) KRW-BTC 마켓에서 1BTC당 1,000 KRW로 거래할 경우, 값은 1000 이 된다.
            ex) KRW-BTC 마켓에서 1BTC당 매도 1호가가 500 KRW 인 경우, 시장가 매수 시 값을 1000으로 세팅하면 2BTC가 매수된다.
            (수수료가 존재하거나 매도 1호가의 수량에 따라 상이할 수 있음)
            ord_type *: 주문 타입 (필수)
            - limit : 지정가 주문
            - price : 시장가 주문(매수)
            - market : 시장가 주문(매도)
            identifier: 조회용 사용자 지정값 (선택)

        response:
            uuid: 주문의 고유 아이디, String
            side: 주문 종류, String
            ord_type: 주문 방식, String
            price: 주문 당시 화폐 가격, NumberString
            avg_price: 체결 가격의 평균가, NumberString
            state: 주문 상태, String
            market: 마켓의 유일키, String
            created_at: 주문 생성 시간, String
            volume: 사용자가 입력한 주문 양, NumberString
            remaining_volume: 체결 후 남은 주문 양, NumberString
            reserved_fee: 수수료로 예약된 비용, NumberString
            remaining_fee: 남은 수수료, NumberString
            paid_fee: 사용된 수수료, NumberString
            locked: 거래에 사용중인 비용, NumberString
            executed_volume: 체결된 양, NumberString
            trade_count: 해당 주문에 걸린 체결 수, Integer
        """
        self.logger.info(f"ORDER ##### {'BUY' if is_buy else 'SELL'}")
        self.logger.info(f"{market}, price: {price}, volume: {volume}")
        if price is not None and volume is not None:
            # 지정가 주문
            if self.is_opt_mode:
                price = self._optimize_price(price, is_buy)
            query_string = self._create_limit_order_query(market, is_buy, price, volume)
        elif volume is not None and is_buy is False:
            # 시장가 매도
            self.logger.warning("### Marker price order is submitted ###")
            query_string = self._create_market_price_order_query(market, volume=volume)
        elif price is not None and is_buy is True:
            # 시장가 매수
            self.logger.warning("### Marker price order is submitted ###")
            query_string = self._create_market_price_order_query(market, price=price)
        else:
            # 잘못된 주문
            self.logger.error("Invalid order")
            return

        jwt_token = self._create_jwt_token(self.ACCESS_KEY, self.SECRET_KEY, query_string)
        authorize_token = "Bearer {}".format(jwt_token)
        headers = {"Authorization": authorize_token}

        try:
            response = requests.post(
                self.SERVER_URL + "/v1/orders", params=query_string, headers=headers
            )
            response.raise_for_status()
            result = response.json()
        except ValueError:
            self.logger.error("Invalid data from server")
            return
        except requests.exceptions.HTTPError as msg:
            self.logger.error(msg)
            return
        except requests.exceptions.RequestException as msg:
            self.logger.error(msg)
            return

        return result

    def _optimize_price(self, price, is_buy):
        latest = self.get_trade_tick()
        if latest is None:
            return price

        if (is_buy is True and latest[0]["trade_price"] < price) or (
            is_buy is False and latest[0]["trade_price"] > price
        ):
            self.logger.info(f"price optimized! ##### {price} -> {latest[0]['trade_price']}")
            return latest[0]["trade_price"]

        return price

    def _create_limit_order_query(self, market, is_buy, price, volume):
        query = {
            "market": market,
            "side": "bid" if is_buy is True else "ask",
            "volume": str(volume),
            "price": str(price),
            "ord_type": "limit",
        }
        query_string = urlencode(query).encode()
        return query_string

    def _create_market_price_order_query(self, market, price=None, volume=None):
        query = {
            "market": market,
        }

        if price is None and volume is not None:
            query["side"] = "ask"
            query["volume"] = str(volume)
            query["ord_type"] = "market"
        elif price is not None and volume is None:
            query["side"] = "bid"
            query["price"] = str(price)
            query["ord_type"] = "price"
        else:
            return

        query_string = urlencode(query).encode()
        return query_string

    def _query_order_list(self, uuids, is_done_state):
        """
        response:
            uuid: 주문의 고유 아이디, String
            side: 주문 종류, String
            ord_type: 주문 방식, String
            price: 주문 당시 화폐 가격, NumberString
            state: 주문 상태, String
            market: 마켓의 유일키, String
            created_at: 주문 생성 시간, DateString
            volume: 사용자가 입력한 주문 양, NumberString
            remaining_volume: 체결 후 남은 주문 양, NumberString
            reserved_fee: 수수료로 예약된 비용, NumberString
            remaining_fee: 남은 수수료, NumberString
            paid_fee: 사용된 수수료, NumberString
            locked: 거래에 사용중인 비용, NumberString
            executed_volume: 체결된 양, NumberString
            trade_count: 해당 주문에 걸린 체결 수, Integer
        """
        query_states = ["wait", "watch"]
        if is_done_state:
            query_states = ["done", "cancel"]

        states_query_string = "&".join(["states[]={}".format(state) for state in query_states])
        uuids_query_string = "&".join(["uuids[]={}".format(uuid) for uuid in uuids])
        query_string = "{0}&{1}".format(states_query_string, uuids_query_string).encode()

        jwt_token = self._create_jwt_token(self.ACCESS_KEY, self.SECRET_KEY, query_string)
        authorize_token = "Bearer {}".format(jwt_token)
        headers = {"Authorization": authorize_token}

        return self._request_get(
            self.SERVER_URL + "/v1/orders", params=query_string, headers=headers
        )

    def _query_account(self):
        """
        response:
            currency: 화폐를 의미하는 영문 대문자 코드, String
            balance: 주문가능 금액/수량, NumberString
            locked: 주문 중 묶여있는 금액/수량, NumberString
            avg_buy_price: 매수평균가, NumberString
            avg_buy_price_modified: 매수평균가 수정 여부, Boolean
            unit_currency: 평단가 기준 화폐, String
        """
        jwt_token = self._create_jwt_token(self.ACCESS_KEY, self.SECRET_KEY)
        authorize_token = "Bearer {}".format(jwt_token)
        headers = {"Authorization": authorize_token}

        return self._request_get(self.SERVER_URL + "/v1/accounts", headers=headers)

    def _request_get(self, url, headers=None, params=None):
        try:
            if params is not None:
                response = requests.get(url, params=params, headers=headers)
            else:
                response = requests.get(url, headers=headers)
            response.raise_for_status()
            result = response.json()
        except ValueError:
            self.logger.error("Invalid data from server")
            return
        except requests.exceptions.HTTPError as msg:
            self.logger.error(msg)
            return
        except requests.exceptions.RequestException as msg:
            self.logger.error(msg)
            return

        return result

    def _create_jwt_token(self, a_key, s_key, query_string=None):
        payload = {
            "access_key": a_key,
            "nonce": str(uuid.uuid4()),
        }
        if query_string is not None:
            m = hashlib.sha512()
            m.update(query_string)
            query_hash = m.hexdigest()
            payload["query_hash"] = query_hash
            payload["query_hash_alg"] = "SHA512"

        return jwt.encode(payload, s_key)

    def _cancel_order(self, uuid):
        """
        request:
            uuid: 취소할 주문의 UUID, String

        response:
            uuid: 주문의 고유 아이디, String
            side: 주문 종류, String
            ord_type: 주문 방식, String
            price: 주문 당시 화폐 가격, NumberString
            avg_price: 체결 가격의 평균가, NumberString
            state: 주문 상태, String
            market: 마켓의 유일키, String
            created_at: 주문 생성 시간, String
            volume: 사용자가 입력한 주문 양, NumberString
            remaining_volume: 체결 후 남은 주문 양, NumberString
            reserved_fee: 수수료로 예약된 비용, NumberString
            remaining_fee: 남은 수수료, NumberString
            paid_fee: 사용된 수수료, NumberString
            locked: 거래에 사용중인 비용, NumberString
            executed_volume: 체결된 양, NumberString
            trade_count: 해당 주문에 걸린 체결 수, Integer
        """
        self.logger.info(f"CANCEL ORDER ##### {uuid}")

        query = {
            "uuid": uuid,
        }
        query_string = urlencode(query).encode()

        jwt_token = self._create_jwt_token(self.ACCESS_KEY, self.SECRET_KEY, query_string)
        authorize_token = "Bearer {}".format(jwt_token)
        headers = {"Authorization": authorize_token}

        try:
            response = requests.delete(
                self.SERVER_URL + "/v1/order", params=query_string, headers=headers
            )
            response.raise_for_status()
            result = response.json()
        except ValueError:
            self.logger.error("Invalid data from server")
            return
        except requests.exceptions.HTTPError as msg:
            self.logger.error(msg)
            return
        except requests.exceptions.RequestException as msg:
            self.logger.error(msg)
            return

        return result