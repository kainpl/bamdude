class TestAbsoluteExtrusionGuard:
    def test_m82_makes_the_layer_parser_decline(self):
        """Absolute E: every E value is a position, so cumulative sums would be
        garbage. The parser declines and the caller's linear fallback covers."""
        from backend.app.utils.threemf_tools import parse_gcode_layer_filament_usage

        gcode = "M82\nM620 S0\nM73 L1\nG1 X1 Y1 E5.0\nG1 X2 Y2 E10.0\n"
        assert parse_gcode_layer_filament_usage(gcode) == {}

    def test_m83_stream_still_parses(self):
        from backend.app.utils.threemf_tools import parse_gcode_layer_filament_usage

        gcode = "M83\nM620 S0\nM73 L1\nG1 X1 Y1 E5.0\nG1 X2 Y2 E5.0\n"
        result = parse_gcode_layer_filament_usage(gcode)
        assert result and result[1][0] == 10.0
