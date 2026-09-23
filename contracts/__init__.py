"""跨進程唯一真相層。

audio-service / inference-service / gateway+ui 三個進程只透過這個套件裡的型別
交換資料。任何一個進程都不應該 import 另一個進程套件（例如 gateway 不該
`from inference.pipeline import ...`）—— 只能 import contracts。

見 ARCHITECTURE.md §5。
"""
