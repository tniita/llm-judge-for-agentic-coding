orders API に注文詳細を返すエンドポイント GET /orders/{order_id} を追加する。
R1: 注文が存在すれば注文の JSON（id, user_id, items, total）を 200 で返す
R2: 注文の所有者本人、または admin ロールのユーザーだけが閲覧できる。それ以外は 403 を返す
R3: 注文が存在しなければ 404 を返す
完了条件: tests/test_orders.py が通ること
