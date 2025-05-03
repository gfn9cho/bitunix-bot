import logging
from logging.handlers import TimedRotatingFileHandler, SMTPHandler
import os

# Standard logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bitunix")

# Error logger setup
error_logger = logging.getLogger("error_logger")
error_logger.setLevel(logging.ERROR)

smtp_handler = SMTPHandler(
    mailhost=("smtp.gmail.com", 587),
    fromaddr=os.getenv("EMAIL_SENDER"),
    toaddrs=["prabha.rec@gmail.com"],
    subject="Bitunix Trading Bot Error Alert",
    credentials=(os.getenv("EMAIL_SENDER"), os.getenv("EMAIL_PASSWORD")),
    secure=()
)
smtp_handler.setLevel(logging.ERROR)
smtp_handler.setFormatter(logging.Formatter('%(asctime)s | %(levelname)s | %(message)s'))
error_logger.addHandler(smtp_handler)

error_handler = TimedRotatingFileHandler("logs/trade_errors.log", when="midnight", interval=1, backupCount=7)
error_handler.setFormatter(logging.Formatter('%(message)s'))
error_logger.addHandler(error_handler)

# Trade logger setup
trade_logger = logging.getLogger("trade_logger")
trade_logger.setLevel(logging.INFO)
handler = TimedRotatingFileHandler("logs/trade_history.log", when="midnight", interval=1, backupCount=7)
handler.setFormatter(logging.Formatter('%(message)s'))
trade_logger.addHandler(handler)

# Reversal logger setup
reversal_logger = logging.getLogger("reversal_logger")
reversal_logger.setLevel(logging.INFO)
reversal_handler = TimedRotatingFileHandler("logs/trade_reversals.log", when="midnight", interval=1, backupCount=7)
reversal_handler.setFormatter(logging.Formatter('%(message)s'))
reversal_logger.addHandler(reversal_handler)
