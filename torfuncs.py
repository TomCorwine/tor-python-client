from StringIO import StringIO
import consensus
import binascii
from collections import namedtuple
import pprint
import os
from OpenSSL import crypto
import time
import ssl,socket,struct
from binascii import hexlify
from Crypto.Hash import SHA
from Crypto.Cipher import *
from Crypto.PublicKey import *
import sys
from Crypto.Util import Counter

# cipher = AES CTR (ZERO IV START)
# HASH = SHA1
# RSA 1024bit, e=65537, OAEP
KEY_LEN=16
DH_LEN=128
DH_SEC_LEN=40
PK_ENC_LEN=128
PK_PAD_LEN=42
HASH_LEN=20
DH_G = 2
DH_P = 179769313486231590770839156793787453197860296048756011706444423684197180216158519368947833795864925541502180565485980503646440548199239100050792877003355816639229553136239076508735759914822574862575007425302077447712589550957937778424442426617334727629299387668709205606050270810842907692932019128194467627007L

cellTypes = {
         1: "CREATE",
         2: "CREATED",
         3: "RELAY",
         4: "DESTROY",
         5: "CREATE_FAST",
         6: "CREATED_FAST",
         8: "NETINFO",
         9: "RELAY_EARLY",
         10: "CREATE2",
         11: "CREATED2",
         7: "VERSIONS",
         128: "VPADDING",
         129: "CERTS",
         130: "AUTH_CHALLENGE",
         131: "AUTHENTICATE",
         132: "AUTHORIZE" }

class TorHop:
    def __init__(self, KH, Df, Db, Kf, Kb):
        self.KH = KH
        self.Df = Df
        self.Db = Db
        self.Kf = Kf
        self.Kb = Kb

        self.fwdSha = SHA.new()
        self.fwdSha.update(Df)
        self.bwdSha = SHA.new()
        self.bwdSha.update(Db)

        ctr = Counter.new(128,initial_value=0)
        self.fwdCipher = AES.new(Kf, AES.MODE_CTR, counter=ctr)
        ctr = Counter.new(128,initial_value=0)
        self.bwdCipher = AES.new(Kb, AES.MODE_CTR, counter=ctr)
    def encrypt(self, data):
        return self.fwdCipher.encrypt(data)
    def decrypt(self, data):
        return self.bwdCipher.decrypt(data)

def cellTypeToId(typ):
    return cellTypes.values().index(typ)

certTypes = {
        1: "LINK",
        2: "RSAIDENT",
        3: "RSA AUTH" }

#according to tor spec, performs hybrid encrypt for create/etc
def hybridEncrypt(rsa, m):
    cipher = PKCS1_OAEP.new(rsa)
    if len(m) < (PK_ENC_LEN - PK_PAD_LEN):
        return cipher.encrypt(m)
    else:
        symkey = os.urandom(KEY_LEN)
        ctr = Counter.new(128, initial_value=0)
        aes = AES.new(symkey, AES.MODE_CTR, counter=ctr)
        m1 = m[0:PK_ENC_LEN-PK_PAD_LEN-KEY_LEN]
        m2 = m[PK_ENC_LEN-PK_PAD_LEN-KEY_LEN:]
        rsapart = cipher.encrypt(symkey+m1)
        sympart = aes.encrypt(m2)
        return rsapart + sympart


#build a cell given circuit, cmdid and payload
def buildCell(circId, cmdId, pl):
    s = struct.pack(">HB", circId, cmdId)
    plen  = 509
    if cmdId == 7 or cmdId > 127:
        plen = len(pl)
    if len(pl) < plen:
        pl += '\x00'*(plen-len(pl))
    return s + pl

#receive a cell and unpack it
def decodeCell(cell):
    io = cell
    if isinstance(cell, basestring):
        io = StringIO(cell)
    hdrbytes = io.read(3)
    circid, cmd = struct.unpack(">HB", hdrbytes)

    if cmd > 127 or cmd == 7: # var length packet
        plenbytes = io.read(2)
        plen = struct.unpack(">H", plenbytes)[0]
    else: #fixed length packet
        plen = 509

    #receive payload
    payload = ''
    while len(payload) != plen:
        payload += io.read(plen-len(payload))

    return (circid, cmd, payload)


#Tor KDF function
def kdf_tor(K0, length):
    K = ''
    i = 0
    while len(K) < length:
        K += SHA.new(K0 + chr(i)).digest()
        i+=1
    return K

#packs a number as big endian into nbytes
def numpack(n, nbytes):
    n2 = hex(n)[2:-1]
    if(len (n2) % 2 != 0) and nbytes != 0:
        n2 = '0' + n2
    n2 = n2.decode('hex')
    return "\x00" * (nbytes - len(n2)) + n2

def numunpack(s):
    return int(s.encode("hex"),16)

#returns private x and created pkt
def buildCreateCell(identityHash, circId):
#get router rsa onion key
    rd = consensus.getRouterDescriptor(identityHash)
    rdk = consensus.getRouterOnionKey(rd)
    rsa = RSA.importKey(rdk)

#generate diffie helman secret
    x = numunpack(os.urandom(DH_SEC_LEN))
#DH pub key X
    X = pow(DH_G, x, DH_P)
#encrypt X to remote
    createpayload = hybridEncrypt(rsa,numpack(X, DH_LEN))
#pack packet
    pkt = struct.pack(">HB", circId, cellTypeToId("CREATE")) + createpayload + "\x00" * (509-len(createpayload))
    return (x, pkt)

def decodeCreatedCell(created, x):
# other side pub key
    Y = created[:DH_LEN]
    derkd = created[DH_LEN:DH_LEN+HASH_LEN]
#compute shared secret
    xy = pow(numunpack(Y), x, DH_P)
#derive shared key data
    KK = StringIO(kdf_tor(numpack(xy, DH_LEN), 3*HASH_LEN + 2*KEY_LEN))
    (KH, Df, Db) = [KK.read(HASH_LEN) for i in range(3)]
    (Kf, Kb) = [KK.read(KEY_LEN) for i in range(2)]
    if derkd == KH:
        return TorHop(KH, Df, Db, Kf, Kb)
    else:
        print "derkd check failed"
        sys.exit(0)

def buildRelayCell(torhop, relCmd, streamId, data):
#construct pkt
    pkt = struct.pack(">BHHLH", relCmd, 0, streamId, 0, len(data)) + data
    pkt += "\x00" * (509 - len(pkt))
#update rolling sha1 hash (with digest set to all zeroes)
    torhop.fwdSha.update(pkt)
#splice in hash
    pkt = pkt[0:5] + torhop.fwdSha.digest()[0:4] + pkt[9:]
    print "relay contents: ", pkt.encode('hex')
#encrypt
    return torhop.encrypt(pkt)

