duration.py に parse_duration(text: str) -> int を追加する。
R1: "1h30m" のような文字列を秒数（この例では 5400）に変換して返す
R2: 単位は h（時間）, m（分）, s（秒）に対応する。単位は任意の組み合わせで、この順に並ぶ
R3: 空文字列や未対応の単位など不正な入力では ValueError を送出する
完了条件: tests/test_duration.py が通ること
