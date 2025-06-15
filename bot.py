# bot.py
import threading
import time
import random
from datetime import datetime
import pandas as pd
from binance_client import BinanceClient, DummyClient
from logger import get_logger
from config import MODE_AUTO, MODE_MANUAL
from risk_manager import RiskManager  # Importa la clase RiskManager

logger = get_logger(__name__)

class BotController:
    def __init__(self, db=None, symbol="BTCUSDT", capital=1000.0, leverage=10):
        self.db = db
        self.symbol = symbol
        self.capital = capital
        self.leverage = leverage
        self.client = BinanceClient(symbol) if self._has_binance_credentials() else DummyClient(symbol)

        self.mode = MODE_MANUAL
        self.position = 0.0
        self.position_entry_price = 0.0
        self.open_orders = []
        self.order_history = []
        self.lock = threading.Lock()
        self.running = False
        self.thread = None
        self.data_collector = None

        # Inicializar RiskManager
        self.risk_manager = RiskManager(db=self.db)

        # Variables para manejo SL/TP dinámico
        self.current_sl = None
        self.current_tp = None

    def _has_binance_credentials(self):
        # Lógica para validar si hay credenciales de Binance configuradas
        return True

    def start(self):
        if self.running:
            logger.warning("Bot ya está corriendo.")
            return
        self.running = True
        logger.info("Bot iniciado.")
        self._log_event("Bot iniciado.")
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()

    def stop(self):
        if not self.running:
            logger.warning("Bot no está corriendo.")
            return
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)
        logger.info("Bot detenido.")
        self._log_event("Bot detenido.")

    def _run_loop(self):
        while self.running:
            try:
                if self.mode == MODE_AUTO and not self.risk_manager.is_locked():
                    self._automatic_trade_logic()
                else:
                    logger.debug("Modo manual o bot bloqueado, esperando...")
                time.sleep(1)
            except Exception as e:
                logger.error(f"Error en loop principal: {e}")
                self._log_event(f"Error en loop principal: {e}", level="error")

    def _automatic_trade_logic(self):
        # Ejemplo simplificado de lógica de trading
        current_price = self.client.get_price()
        atr = self._calculate_atr()
        rsi = self.client.get_rsi(self.symbol, '5m')

        trend_confirmed = True  # Simplificado, en realidad debe calcularse
        if self.risk_manager.should_cancel_trade(rsi, trend_confirmed):
            logger.info("Trade cancelado por condiciones RSI y tendencia")
            return

        # Lógica simple para abrir posición
        if self.position == 0 and self.risk_manager.check_open_positions(len(self.open_orders)):
            if rsi < 30:
                if self.risk_manager.check_position_size(self.capital * 0.01, self.capital):
                    self._open_position('BUY')
            elif rsi > 70:
                if self.risk_manager.check_position_size(self.capital * 0.01, self.capital):
                    self._open_position('SELL')

        # Actualizar SL/TP si hay posición abierta
        if self.position != 0:
            sl, tp = self.risk_manager.calculate_dynamic_sl_tp(
                self.position_entry_price,
                atr,
                'long' if self.position > 0 else 'short'
            )
            self.current_sl, self.current_tp = sl, tp
            self.risk_manager.update_sl_tp_if_trend_continues(
                current_price,
                self.current_sl,
                self.current_tp,
                'long' if self.position > 0 else 'short',
                atr
            )
    def _calculate_atr(self, period=14):
        """
        Calcula el ATR basado en datos históricos de velas.
        Retorna un valor float.
        """
        try:
            candles = self.client.get_historical_klines(self.symbol, '1h', f'{period + 1} hours ago UTC')
            if len(candles) < period + 1:
                return 0.0
            highs = [float(c[2]) for c in candles]
            lows = [float(c[3]) for c in candles]
            closes = [float(c[4]) for c in candles]

            trs = []
            for i in range(1, len(candles)):
                tr = max(
                    highs[i] - lows[i],
                    abs(highs[i] - closes[i - 1]),
                    abs(lows[i] - closes[i - 1])
                )
                trs.append(tr)

            atr = sum(trs[-period:]) / period
            return atr
        except Exception as e:
            logger.error(f"Error calculando ATR: {e}")
            return 0.0

    def _open_position(self, side):
        with self.lock:
            # Simplificado: abrir posición con tamaño fijo
            size = self.capital * 0.01  # 1% del capital
            price = self.client.get_price()

            # Verificar bloqueo de riesgo antes de abrir
            if self.risk_manager.is_locked():
                logger.warning(f"Apertura de posición bloqueada por riesgo: {self.risk_manager.get_lock_reason()}")
                self._log_event(f"Apertura bloqueada por riesgo: {self.risk_manager.get_lock_reason()}")
                return False

            # Abrir posición larga o corta
            if side == "BUY":
                self.position += size
                self.position_entry_price = price if self.position_entry_price == 0 else self.position_entry_price
                self.capital -= size * price  # Ajustar capital
            elif side == "SELL":
                self.position -= size
                self.position_entry_price = price if self.position_entry_price == 0 else self.position_entry_price
                self.capital += size * price

            # Actualizar riesgo
            self.risk_manager.update_on_trade(0, self.capital)

            # Guardar orden abierta
            order = {
                "orderId": random.randint(100000, 999999),
                "symbol": self.symbol,
                "side": side,
                "type": "OPEN",
                "origQty": size,
                "price": price,
                "status": "FILLED",
                "pl": 0,
                "time": int(time.time() * 1000),
                "created_at": datetime.utcnow().isoformat(),
            }
            self.open_orders.append(order)
            self.order_history.append(order)

            try:
                if self.db:
                    self.db.insert_order(order)
                    self.db.update_position(self.symbol, self.position, self.position_entry_price)
            except Exception as e:
                logger.error(f"Error guardando orden en BD: {e}")
                self._log_event(f"Error guardando orden en BD: {e}", level="error")

            logger.info(f"Posición abierta: {side} {size} a {price}")
            self._log_event(f"Posición abierta: {side} {size} a {price}")
            return True

    def _close_position(self, side, size):
        with self.lock:
            if size > abs(self.position):
                logger.warning(f"Intento de cerrar posición mayor al tamaño actual: {size} > {abs(self.position)}")
                return False

            price = self.client.get_price()
            profit_loss = 0.0

            if side == "SELL" and self.position > 0:
                # Cerrar posición larga parcial o total
                profit_loss = size * (price - self.position_entry_price)
                self.position -= size
                self.capital += size * price
            elif side == "BUY" and self.position < 0:
                # Cerrar posición corta parcial o total
                profit_loss = size * (self.position_entry_price - price)
                self.position += size
                self.capital += size * (2 * self.position_entry_price - price)  # Ajuste simplificado

            # Actualizar riesgo con P/L de cierre
            self.risk_manager.update_on_trade(profit_loss, self.capital)

            # Actualizar SL/TP dinámico si queda posición abierta
            if self.position != 0:
                atr_value = self._calculate_atr()
                direction = 'long' if self.position > 0 else 'short'
                sl, tp = self.risk_manager.calculate_dynamic_sl_tp(self.position_entry_price, atr_value, direction)
                self.current_sl = sl
                self.current_tp = tp
            else:
                self.current_sl = None
                self.current_tp = None
                self.position_entry_price = 0.0

            # Actualizar historial y DB
            order = {
                "orderId": random.randint(100000, 999999),
                "symbol": self.symbol,
                "side": side,
                "type": "CLOSE",
                "origQty": size,
                "price": price,
                "status": "FILLED",
                "pl": profit_loss,
                "time": int(time.time() * 1000),
                "created_at": datetime.utcnow().isoformat(),
            }
            self.open_orders = [o for o in self.open_orders if o["status"] == "FILLED" and o["side"] != side]
            self.order_history.append(order)

            try:
                if self.db:
                    self.db.insert_order(order)
                    self.db.update_position(self.symbol, self.position, self.position_entry_price)
            except Exception as e:
                logger.error(f"Error guardando orden en BD: {e}")
                self._log_event(f"Error guardando orden en BD: {e}", level="error")

            logger.info(f"Posición cerrada: {side} {size} a {price} P/L={profit_loss:.2f}")
            self._log_event(f"Posición cerrada: {side} {size} a {price} P/L={profit_loss:.2f}")
            return True
    def execute_manual_order(self, side, size):
        """
        Ejecutar orden manual para abrir o cerrar posición.
        """
        logger.info(f"Ejecutando orden manual: {side} {size}")
        if side.upper() in ["BUY", "SELL"]:
            if (side.upper() == "BUY" and self.position < 0) or (side.upper() == "SELL" and self.position > 0):
                # Cerrar posición contraria
                return self._close_position(side.upper(), size)
            else:
                # Abrir o aumentar posición
                return self._open_position(side.upper())
        else:
            logger.warning(f"Orden manual inválida: {side}")
            return False

    def update_config(self, symbol=None, capital=None, leverage=None, mode=None):
        with self.lock:
            if symbol:
                self.symbol = symbol
            if capital:
                self.capital = capital
            if leverage:
                self.leverage = leverage
            if mode in [MODE_AUTO, MODE_MANUAL]:
                self.mode = mode
            logger.info(f"Configuración actualizada: symbol={self.symbol}, capital={self.capital}, leverage={self.leverage}, mode={self.mode}")
            self._log_event(f"Configuración actualizada: {self.symbol}, capital={self.capital}, leverage={self.leverage}, modo={self.mode}")

    def get_status(self):
        return {
            "running": self.running,
            "mode": self.mode,
            "symbol": self.symbol,
            "capital": self.capital,
            "leverage": self.leverage,
            "position": self.position,
            "entry_price": self.position_entry_price,
            "open_orders": self.open_orders,
            "order_history": self.order_history[-20:],  # últimos 20
            "current_sl": self.current_sl,
            "current_tp": self.current_tp,
            "risk_locked": self.risk_manager.is_locked(),
            "risk_lock_reason": self.risk_manager.get_lock_reason()
        }

    def _log_event(self, message, level="info"):
        if self.db:
            try:
                self.db.insert_log({
                    "timestamp": datetime.utcnow().isoformat(),
                    "level": level,
                    "message": message,
                    "symbol": self.symbol
                })
            except Exception as e:
                logger.error(f"Error guardando log en BD: {e}")

    def run_backtest(self, start_date, end_date, symbol, initial_capital):
        """
        Método simplificado de backtesting:
        - Carga datos históricos
        - Simula operaciones con la estrategia
        - Actualiza capital y genera curva de equity
        - Guarda resultados en DB
        """
        logger.info(f"Ejecutando backtest {symbol} desde {start_date} hasta {end_date}")
        capital = initial_capital
        equity_curve = []
        try:
            candles = self.client.get_historical_klines(symbol, '5m', f"{start_date} UTC", f"{end_date} UTC")
            for candle in candles:
                # Ejemplo muy simplificado: solo guarda capital constante
                equity_curve.append(capital)
            # Guardar resultado en DB
            if self.db:
                self.db.insert_backtest_result({
                    "symbol": symbol,
                    "start_date": start_date,
                    "end_date": end_date,
                    "equity_curve": equity_curve,
                })
            logger.info("Backtest completado")
            return equity_curve
        except Exception as e:
            logger.error(f"Error en backtest: {e}")
            return []
    def start(self):
        if self.running:
            logger.warning("Bot ya está corriendo")
            return
        self.running = True
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        logger.info("Bot iniciado")
        self._log_event("Bot iniciado")

    def stop(self):
        if not self.running:
            logger.warning("Bot ya está detenido")
            return
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)
            logger.info("Bot detenido")
            self._log_event("Bot detenido")

    def _run_loop(self):
        while self.running:
            if self.mode == MODE_AUTO and not self.risk_manager.is_locked():
                # Ejecutar lógica automática aquí
                self._automatic_trade_logic()
            else:
                logger.debug("Modo manual o bot bloqueado, esperando...")
            time.sleep(1)

    def _automatic_trade_logic(self):
        # Ejemplo simplificado de lógica de trading
        current_price = self.client.get_price()
        atr = self._calculate_atr()
        rsi = self.client.get_rsi(self.symbol, '5m')

        trend_confirmed = True  # Simplificado, en realidad debe calcularse
        if self.risk_manager.should_cancel_trade(rsi, trend_confirmed):
            logger.info("Trade cancelado por condiciones RSI y tendencia")
            return

        # Lógica simple para abrir posición
        if self.position == 0 and self.risk_manager.check_open_positions(len(self.open_orders)):
            if rsi < 30:
                if self.risk_manager.check_position_size(self.capital * 0.01, self.capital):
                    self._open_position('BUY')
            elif rsi > 70:
                if self.risk_manager.check_position_size(self.capital * 0.01, self.capital):
                    self._open_position('SELL')

        # Actualizar SL/TP si hay posición abierta
        if self.position != 0:
            sl, tp = self.risk_manager.calculate_dynamic_sl_tp(self.position_entry_price, atr, 'long' if self.position > 0 else 'short')
            self.current_sl, self.current_tp = sl, tp
            self.risk_manager.update_sl_tp_if_trend_continues(current_price, self.current_sl, self.current_tp, 'long' if self.position > 0 else 'short', atr)

    def _has_binance_credentials(self):
        # Implementar chequeo real de credenciales Binance
        return True
    def _calculate_atr(self, period=14):
        """
        Calcula el ATR basado en datos históricos de velas.
        Retorna un valor float.
        """
        try:
            candles = self.client.get_historical_klines(self.symbol, '1h', f'{period + 1} hours ago UTC')
            if len(candles) < period + 1:
                return 0.0
            highs = [float(c[2]) for c in candles]
            lows = [float(c[3]) for c in candles]
            closes = [float(c[4]) for c in candles]

            trs = []
            for i in range(1, len(candles)):
                tr = max(
                    highs[i] - lows[i],
                    abs(highs[i] - closes[i - 1]),
                    abs(lows[i] - closes[i - 1])
                )
                trs.append(tr)

            atr = sum(trs[-period:]) / period
            return atr
        except Exception as e:
            logger.error(f"Error calculando ATR: {e}")
            return 0.0
    def run_backtest(self, start_date, end_date, symbol, initial_capital):
        """
        Método simplificado de backtesting:
        - Carga datos históricos
        - Simula operaciones con la estrategia
        - Actualiza capital y genera curva de equity
        - Guarda resultados en DB
        """
        logger.info(f"Ejecutando backtest {symbol} desde {start_date} hasta {end_date}")
        capital = initial_capital
        equity_curve = []
        try:
            candles = self.client.get_historical_klines(symbol, '5m', f"{start_date} UTC", f"{end_date} UTC")
            for candle in candles:
                # Ejemplo muy simplificado: solo guarda capital constante
                equity_curve.append(capital)
            # Guardar resultado en DB
            if self.db:
                self.db.insert_backtest_result({
                    "symbol": symbol,
                    "start_date": start_date,
                    "end_date": end_date,
                    "equity_curve": equity_curve,
                })
            logger.info("Backtest completado")
            return equity_curve
        except Exception as e:
            logger.error(f"Error en backtest: {e}")
            return []
