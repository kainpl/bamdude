; HEADER_BLOCK_START
; generated for BamDude part-render tests
; model label id: 101,202
; HEADER_BLOCK_END
; filament_colour = #C0C0C0;#161616;#00AE42
M83
G90
T0
G1 X18 Y1 Z0.8 F18000
G1 X218 Y1 E12 F1200
; CHANGE_LAYER
; Z_HEIGHT: 0.2
G1 Z0.2 F600
; FEATURE: Skirt
; LINE_WIDTH: 0.5
G1 X95 Y95 F12000
G1 X135 Y95 E1.2
G1 X135 Y115 E0.6
G1 X95 Y115 E1.2
G1 X95 Y95 E0.6
; start printing object, unique label id: 101
; FEATURE: Outer wall
; LINE_WIDTH: 0.42
G1 X100 Y100 F12000
G1 X110 Y100 E0.4
G1 X110 Y110 E0.4
G1 X100 Y110 E0.4
G1 X100 Y100 E0.4
T2
; FEATURE: Top surface
G1 X103 Y103 F12000
G1 X107 Y107 E0.2
T0
; stop printing object, unique label id: 101
; start printing object, unique label id: 202
; FEATURE: Outer wall
G1 X125 Y105 F12000
G2 X125 Y105 I5 J0 E1.2
; stop printing object, unique label id: 202
; CHANGE_LAYER
; Z_HEIGHT: 0.4
G1 Z0.4 F600
; start printing object, unique label id: 101
; FEATURE: Outer wall
G1 X100 Y100 F12000
G1 X110 Y100 E0.4
G1 X110 Y110 E0.4
; stop printing object, unique label id: 101
; start printing object, unique label id: 202
; FEATURE: Support
G1 X126 Y104 E0.3
; FEATURE: Outer wall
G1 X125 Y105 F12000
G2 X125 Y105 I5 J0 E1.2
; stop printing object, unique label id: 202
; FEATURE: Prime tower
G1 X200 Y200 F12000
G1 X210 Y200 E0.5
