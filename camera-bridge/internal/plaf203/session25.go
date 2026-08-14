package plaf203

import "encoding/binary"

const (
	session25CountersLength = 24
	session25CounterCommand = 0x09
	session25Version        = 0x0B
	session25InitialPacket  = 0x3FFF
)

// session25Counters is the bounded flow-control state carried by a Session25
// 0x0900 packet. It contains no device identity or credential material.
type session25Counters struct {
	seqSendCmd1 uint16
	seqRecvPkt0 uint16
	seqRecvPkt1 uint16
	seqRecvCmd2 uint16
	seqSendCnt  uint16
	random      uint16
}

// session25State holds the client-side counters required by the PLAF203
// Session25 flow-control exchange. The state intentionally mirrors the
// official go2rtc implementation instead of replaying a constant packet.
type session25State struct {
	seqSendCmd1 uint16
	seqSendCmd2 uint16
	seqRecvCmd2 uint16
	seqRecvPkt0 uint16
	seqRecvPkt1 uint16
	seqSendCnt  uint16
}

func newSession25State() session25State {
	return session25State{
		seqRecvPkt0: session25InitialPacket,
		seqRecvPkt1: session25InitialPacket,
	}
}

// nextOutboundCommand returns the command sequence shared by PLAF203 Session25
// heartbeats and IOCtrl packets, then advances both command sequence spaces.
func (state *session25State) nextOutboundCommand() uint16 {
	sequence := state.seqSendCmd1
	state.seqSendCmd1++
	state.seqSendCmd2++
	return sequence
}

// nextCounters serializes msgAckCounters using the same counter transitions as
// the official TUTK Session25 implementation. random is the low 16 bits of
// the current millisecond clock on production sends.
func (state *session25State) nextCounters(random uint16) ([]byte, session25Counters) {
	counters := session25Counters{
		seqSendCmd1: state.seqSendCmd1,
		seqRecvPkt0: state.seqRecvPkt0,
		seqRecvPkt1: state.seqRecvPkt1,
		seqRecvCmd2: state.seqRecvCmd2,
		seqSendCnt:  state.seqSendCnt,
		random:      random,
	}
	body := encodeSession25Counters(counters)
	state.seqSendCmd1++
	state.seqRecvPkt0 = state.seqRecvPkt1
	state.seqSendCnt++
	if state.seqSendCnt == 1 {
		// The first PLAF203 counters frame reports recvCmd2=0 and advances the
		// following frame to the protocol's initial 0xFFFF sentinel.
		state.seqRecvCmd2 = 0xFFFF
	}
	return body, counters
}

// noteReceivedControlReply advances the command acknowledgement cursor. The
// initial 0xFFFF sentinel transitions to one after the first device control
// response, matching the captured official PLAF203 Session25 exchange.
func (state *session25State) noteReceivedControlReply() {
	if state.seqRecvCmd2 == 0xFFFF {
		state.seqRecvCmd2 = 1
		return
	}
	state.seqRecvCmd2++
}

// noteReceivedMedia records the last completed media packet sequence. It is
// emitted in the next counters acknowledgement, exactly as Session25 requires.
func (state *session25State) noteReceivedMedia(sequence uint16) {
	state.seqRecvPkt1 = sequence
}

func decodeSession25Counters(body []byte) (session25Counters, bool) {
	if len(body) != session25CountersLength || body[0] != session25CounterCommand || body[1] != 0 || body[2] != session25Version || body[3] != 0 {
		return session25Counters{}, false
	}
	return session25Counters{
		seqSendCmd1: binary.LittleEndian.Uint16(body[4:6]),
		seqRecvPkt0: binary.LittleEndian.Uint16(body[8:10]),
		seqRecvPkt1: binary.LittleEndian.Uint16(body[10:12]),
		seqRecvCmd2: binary.LittleEndian.Uint16(body[12:14]),
		seqSendCnt:  binary.LittleEndian.Uint16(body[18:20]),
		random:      binary.LittleEndian.Uint16(body[20:22]),
	}, true
}

func encodeSession25Counters(counters session25Counters) []byte {
	body := make([]byte, session25CountersLength)
	body[0] = session25CounterCommand
	body[2] = session25Version
	binary.LittleEndian.PutUint16(body[4:6], counters.seqSendCmd1)
	binary.LittleEndian.PutUint16(body[8:10], counters.seqRecvPkt0)
	binary.LittleEndian.PutUint16(body[10:12], counters.seqRecvPkt1)
	binary.LittleEndian.PutUint16(body[12:14], counters.seqRecvCmd2)
	binary.LittleEndian.PutUint16(body[18:20], counters.seqSendCnt)
	binary.LittleEndian.PutUint16(body[20:22], counters.random)
	return body
}
