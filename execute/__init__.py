"""Trade execution: the Broker interface, the paper broker, the live Kalshi and Polymarket US
brokers, and risk rails with a kill switch. PaperBroker is the default and never touches a
venue. The live brokers place real orders once credentials are set, and their place_order()
and unwind() refuse (ERROR, nothing sent) unless LIVE_TRADING_ENABLED is true. The kill switch
lives in RiskManager; the brokers do not consult it, so route every order through risk.check()."""
