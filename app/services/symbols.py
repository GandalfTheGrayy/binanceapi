def normalize_tv_symbol(tv_symbol: str) -> str:
	if not tv_symbol:
		return tv_symbol
	s = tv_symbol.strip().upper()
	# Drop exchange prefix like BINANCE:
	if ":" in s:
		s = s.split(":", 1)[1]
	# Remove common futures suffixes
	for suf in (".P", ".PERP", "PERP"):
		if s.endswith(suf):
			s = s[: -len(suf)]
	# Some platforms use USDT-PERP or USDTPERP
	for rep in ("-PERP", "USDTPERP"):
		if s.endswith(rep):
			s = s.replace(rep, "USDT")
	return s
