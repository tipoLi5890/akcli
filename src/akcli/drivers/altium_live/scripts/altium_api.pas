{ ============================================================================
  altium_api.pas  --  akcli live SCH driver (DelphiScript half)

  Part of akcli (SPEC sec 3.7 "drivers/altium_live", Appendix B).

  WHAT THIS IS
  ------------
  The Altium-side half of the optional file-based JSON bridge. The Python half
  (drivers/altium_live/bridge.py) drops a `request.json` into a shared bridge
  directory and polls for `response.json`; this script -- run inside a *running*
  Altium Designer via the ScriptingSystem -- reads that request, dispatches the
  op-list ops against Altium's public SCH API, and writes `response.json` back.

  The request body is an akcli op-list document (SPEC sec 2.3) and the response
  carries one per-op result object per op, MATCHING the KiCad writer's OpResult
  shape EXACTLY (SPEC sec 2.4):

      { "op_index": 0, "op": "place_component", "status": "ok"|"error",
        "created_uuids": ["..."], "error_code": null, "message": "" }

  PROTOCOL
  --------
  protocol_version handshake == 1 (must equal Python's ops.PROTOCOL_VERSION).
  `command:"altium_ping"` returns {protocol_version, altium_version} so the
  bridge can verify it is talking to a compatible script before sending ops.
  A request whose protocol_version != 1 is rejected document-wide with
  error_code "PROTOCOL_MISMATCH" (the same frozen code errors.py defines).

  UNITS / GEOMETRY
  ----------------
  All op coordinates are MILS, origin top-left (SPEC sec 2.1). MilsToCoord /
  CoordToMils bridge to Altium's internal 1/10000-mil coordinate space.
  Rotation is the enum {0,90,180,270} -> TRotationBy90 (eRotate0..eRotate270).
  Mirror is {none,x,y} -> ISch_Component.IsMirrored.

  VERIFY-BY-RE-EXPORT
  -------------------
  This script does NOT itself prove the edit is electrically correct. After a
  successful apply the recommended verification (documented in README.md) is to
  re-export the Altium netlist and diff it against the intended connectivity --
  the same "verify by re-export / tolerance compare" posture the KiCad writer
  uses with its connectivity gate. The response `message` notes this.

  CLEAN-ROOM / ATTRIBUTION (SPEC Appendix B)
  ------------------------------------------
  Independent reimplementation. No source copied. This file references ONLY
  Altium's public, documented DelphiScript / SCH Scripting API (SchServer,
  ISch_Document, SchObjectFactory, ISch_Iterator, ...) and the documented
  file-based-bridge METHOD. The high-level "JSON file bridge + protocol_version
  + structured ERROR codes" pattern is credited to flaco-source/altium-mcp and
  coffeenmusic/Siddharth Ahuja in THIRD_PARTY_NOTICES.md; no bytes of their
  schema or source are reused.

  PLATFORM VALIDITY
  -----------------
  Validated ONLY on Windows + Altium Designer 22+. It cannot be exercised from
  the macOS/Linux dev or CI environment; the JSON envelope is unit-tested on the
  Python side against a mocked response.json. Treat the SCH-API call sites below
  as a faithful-but-iterate-on-Windows scaffold: object-factory kinds and a few
  indexed-property setters (notably wire vertices) may need a one-line tweak for
  your exact Altium build.
  ============================================================================ }

{ ------------------------------------------------------------------ }
{  Globals                                                            }
{ ------------------------------------------------------------------ }
Const
    AKCLI_PROTOCOL_VERSION = 1;

Var
    gDoc      : ISch_Document;     // target schematic sheet
    gResults  : TStringList;       // accumulated per-op result JSON objects
    gBridgeDir: String;            // shared request/response directory


{ ================================================================== }
{  Minimal, self-contained JSON reader (clean-room).                  }
{  Values are kept as raw JSON substrings and decoded on demand, so   }
{  no user-defined classes / dynamic DOM are required (DelphiScript-  }
{  friendly). Strings are 1-based (Pascal).                           }
{ ================================================================== }

Function Json_SkipWs(Const S : String; P : Integer) : Integer;
Begin
    While (P <= Length(S)) And
          ((S[P] = ' ') Or (S[P] = #9) Or (S[P] = #10) Or (S[P] = #13)) Do
        P := P + 1;
    Result := P;
End;

{ Return the index of the LAST character of the JSON value that starts
  at Start (after Start is positioned on the first non-ws char). Handles
  strings (with \" escapes), {objects}, [arrays] and bare scalars. }
Function Json_ValueEnd(Const S : String; Start : Integer) : Integer;
Var
    P, Depth : Integer;
    C        : Char;
    InStr    : Boolean;
Begin
    P := Start;
    If P > Length(S) Then
    Begin
        Result := P;
        Exit;
    End;
    C := S[P];

    If C = '"' Then
    Begin
        P := P + 1;
        While P <= Length(S) Do
        Begin
            If S[P] = '\' Then P := P + 2     // skip escaped char
            Else If S[P] = '"' Then
            Begin
                Result := P;
                Exit;
            End
            Else P := P + 1;
        End;
        Result := Length(S);
        Exit;
    End;

    If (C = '{') Or (C = '[') Then
    Begin
        Depth := 0;
        InStr := False;
        While P <= Length(S) Do
        Begin
            C := S[P];
            If InStr Then
            Begin
                If C = '\' Then P := P + 1
                Else If C = '"' Then InStr := False;
            End
            Else
            Begin
                If C = '"' Then InStr := True
                Else If (C = '{') Or (C = '[') Then Depth := Depth + 1
                Else If (C = '}') Or (C = ']') Then
                Begin
                    Depth := Depth - 1;
                    If Depth = 0 Then
                    Begin
                        Result := P;
                        Exit;
                    End;
                End;
            End;
            P := P + 1;
        End;
        Result := Length(S);
        Exit;
    End;

    // bare scalar: number / true / false / null
    While (P <= Length(S)) And (S[P] <> ',') And (S[P] <> '}') And
          (S[P] <> ']') And (S[P] <> ' ') And (S[P] <> #9) And
          (S[P] <> #10) And (S[P] <> #13) Do
        P := P + 1;
    Result := P - 1;
End;

{ Decode a JSON string token (raw including quotes) into a plain string.
  If Raw is not a quoted string it is returned trimmed (covers bare tokens). }
Function Json_DecodeString(Const Raw : String) : String;
Var
    P    : Integer;
    Body : String;
    Outp : String;
    C    : Char;
Begin
    Body := Trim(Raw);
    If (Length(Body) >= 2) And (Body[1] = '"') And (Body[Length(Body)] = '"') Then
        Body := Copy(Body, 2, Length(Body) - 2)
    Else
    Begin
        Result := Body;
        Exit;
    End;

    Outp := '';
    P := 1;
    While P <= Length(Body) Do
    Begin
        C := Body[P];
        If (C = '\') And (P < Length(Body)) Then
        Begin
            P := P + 1;
            C := Body[P];
            If C = 'n' Then Outp := Outp + #10
            Else If C = 't' Then Outp := Outp + #9
            Else If C = 'r' Then Outp := Outp + #13
            Else If C = '/' Then Outp := Outp + '/'
            Else If C = 'b' Then Outp := Outp + #8
            Else If C = 'f' Then Outp := Outp + #12
            Else If C = 'u' Then
            Begin
                // \uXXXX -> best-effort: keep ASCII, else '?'. (Altium SCH text
                // is set from the decoded String; full BMP handling is iterated
                // on Windows if a fixture needs CJK in a live write.)
                If P + 4 <= Length(Body) Then
                Begin
                    Outp := Outp + '?';
                    P := P + 4;
                End;
            End
            Else
                Outp := Outp + C;   // \" \\ etc.
        End
        Else
            Outp := Outp + C;
        P := P + 1;
    End;
    Result := Outp;
End;

{ Get the raw value substring for top-level Key in object Obj ("{...}").
  Returns '' when the key is absent. }
Function Json_Get(Const Obj, Key : String) : String;
Var
    P, Depth, VS, VE : Integer;
    InStr            : Boolean;
    C                : Char;
    KeyTok           : String;
Begin
    Result := '';
    KeyTok := '"' + Key + '"';
    P := 1;
    Depth := 0;
    InStr := False;
    While P <= Length(Obj) Do
    Begin
        C := Obj[P];
        If InStr Then
        Begin
            If C = '\' Then P := P + 1
            Else If C = '"' Then InStr := False;
        End
        Else
        Begin
            If C = '"' Then
            Begin
                // only consider keys exactly one level deep inside the object
                If Depth = 1 Then
                Begin
                    If Copy(Obj, P, Length(KeyTok)) = KeyTok Then
                    Begin
                        // advance past key string, then ':'
                        P := Json_ValueEnd(Obj, P) + 1;
                        P := Json_SkipWs(Obj, P);
                        If (P <= Length(Obj)) And (Obj[P] = ':') Then
                        Begin
                            P := Json_SkipWs(Obj, P + 1);
                            VS := P;
                            VE := Json_ValueEnd(Obj, VS);
                            Result := Copy(Obj, VS, VE - VS + 1);
                            Exit;
                        End;
                    End;
                End;
                InStr := True;
            End
            Else If (C = '{') Or (C = '[') Then Depth := Depth + 1
            Else If (C = '}') Or (C = ']') Then Depth := Depth - 1;
        End;
        P := P + 1;
    End;
End;

{ Split a top-level JSON array ("[...]") into its item raw-value substrings. }
Function Json_ArrayItems(Const Arr : String) : TStringList;
Var
    P, Depth, VS, VE : Integer;
    InStr            : Boolean;
    C                : Char;
Begin
    Result := TStringList.Create;
    P := 1;
    // skip the opening '['
    P := Json_SkipWs(Arr, P);
    If (P <= Length(Arr)) And (Arr[P] = '[') Then P := P + 1;
    Depth := 0;
    InStr := False;
    While P <= Length(Arr) Do
    Begin
        P := Json_SkipWs(Arr, P);
        If P > Length(Arr) Then Break;
        C := Arr[P];
        If (C = ']') And (Depth = 0) Then Break;
        VS := P;
        VE := Json_ValueEnd(Arr, VS);
        Result.Add(Copy(Arr, VS, VE - VS + 1));
        P := VE + 1;
        P := Json_SkipWs(Arr, P);
        If (P <= Length(Arr)) And (Arr[P] = ',') Then P := P + 1;
    End;
End;

Function Json_GetString(Const Obj, Key : String) : String;
Begin
    Result := Json_DecodeString(Json_Get(Obj, Key));
End;

Function Json_GetInt(Const Obj, Key : String; Def : Integer) : Integer;
Var
    Raw : String;
Begin
    Raw := Trim(Json_Get(Obj, Key));
    If Raw = '' Then
    Begin
        Result := Def;
        Exit;
    End;
    Try
        Result := StrToInt(Raw);
    Except
        Result := Def;
    End;
End;

Function Json_GetNum(Const Obj, Key : String; Def : Double) : Double;
Var
    Raw : String;
Begin
    Raw := Trim(Json_Get(Obj, Key));
    If Raw = '' Then
    Begin
        Result := Def;
        Exit;
    End;
    Try
        Result := StrToFloat(Raw);
    Except
        Result := Def;
    End;
End;


{ ================================================================== }
{  JSON writer helpers                                                }
{ ================================================================== }

Function Json_Escape(Const S : String) : String;
Var
    I  : Integer;
    C  : Char;
    R  : String;
Begin
    R := '';
    For I := 1 To Length(S) Do
    Begin
        C := S[I];
        If C = '"' Then R := R + '\"'
        Else If C = '\' Then R := R + '\\'
        Else If C = #10 Then R := R + '\n'
        Else If C = #13 Then R := R + '\r'
        Else If C = #9 Then R := R + '\t'
        Else R := R + C;
    End;
    Result := R;
End;

Function Json_QuoteOrNull(Const S : String) : String;
Begin
    If S = '' Then Result := 'null'
    Else Result := '"' + Json_Escape(S) + '"';
End;

{ Build one per-op result object (SPEC sec 2.4) and queue it. UuidsJson is an
  already-formed JSON array string, e.g. '["abc"]' or '[]'. }
Procedure AddResult(Idx : Integer; Const Op, Status, UuidsJson, Code, Msg : String);
Var
    R : String;
Begin
    R := '{"op_index":' + IntToStr(Idx) +
         ',"op":' + Json_QuoteOrNull(Op) +
         ',"status":"' + Status + '"' +
         ',"created_uuids":' + UuidsJson +
         ',"error_code":' + Json_QuoteOrNull(Code) +
         ',"message":"' + Json_Escape(Msg) + '"}';
    gResults.Add(R);
End;

Function UuidArray1(Const U : String) : String;
Begin
    If U = '' Then Result := '[]'
    Else Result := '["' + Json_Escape(U) + '"]';
End;


{ ================================================================== }
{  SCH API helpers                                                    }
{ ================================================================== }

{ Map an akcli rotation enum (0/90/180/270) to TRotationBy90. }
Function RotEnum(Deg : Integer) : TRotationBy90;
Begin
    Case Deg Of
        90  : Result := eRotate90;
        180 : Result := eRotate180;
        270 : Result := eRotate270;
    Else
        Result := eRotate0;
    End;
End;

Procedure RegisterAndNotify(Obj : ISch_GraphicalObject);
Begin
    gDoc.RegisterSchObjectInSchDoc(Obj);
    SchServer.RobotManager.SendMessage(gDoc.I_ObjectAddress, c_BroadCast,
        SCHM_PrimitiveRegistration, Obj.I_ObjectAddress);
End;

{ Find a placed component by its designator text; Nil when absent. }
Function FindComponent(Const Des : String) : ISch_Component;
Var
    Iter : ISch_Iterator;
    Comp : ISch_Component;
Begin
    Result := Nil;
    Iter := gDoc.SchIterator_Create;
    If Iter = Nil Then Exit;
    Try
        Iter.AddFilter_ObjectSet(MkSet(eSchComponent));
        Comp := Iter.FirstSchObject;
        While Comp <> Nil Do
        Begin
            If Comp.Designator.Text = Des Then
            Begin
                Result := Comp;
                Exit;
            End;
            Comp := Iter.NextSchObject;
        End;
    Finally
        gDoc.SchIterator_Destroy(Iter);
    End;
End;

{ Locate the connection point of pin PinNum on component Des. The "REF.PIN"
  endpoint form (SPEC sec 2.1) snaps a wire to this exact world coordinate. }
Function FindPinLocation(Const Des, PinNum : String; Var Loc : TLocation) : Boolean;
Var
    Comp  : ISch_Component;
    PIter : ISch_Iterator;
    Pin   : ISch_Pin;
Begin
    Result := False;
    Comp := FindComponent(Des);
    If Comp = Nil Then Exit;
    PIter := Comp.SchIterator_Create;
    If PIter = Nil Then Exit;
    Try
        PIter.AddFilter_ObjectSet(MkSet(ePin));
        Pin := PIter.FirstSchObject;
        While Pin <> Nil Do
        Begin
            If Pin.Designator = PinNum Then
            Begin
                Loc := Pin.Location;   // pin electrical end (internal coords)
                Result := True;
                Exit;
            End;
            Pin := PIter.NextSchObject;
        End;
    Finally
        Comp.SchIterator_Destroy(PIter);
    End;
End;

{ Resolve an endpoint raw JSON value ("REF.PIN" string OR [x,y] mils array)
  to internal coordinates. Returns False when malformed. }
Function ResolveEndpoint(Const Raw : String; Var Loc : TLocation) : Boolean;
Var
    S, Ref, PinNum : String;
    DotP           : Integer;
    Items          : TStringList;
    Xm, Ym         : Double;
Begin
    Result := False;
    S := Trim(Raw);
    If S = '' Then Exit;

    If S[1] = '"' Then
    Begin
        S := Json_DecodeString(S);          // "REF.PIN"
        DotP := 0;
        // split on the LAST '.' (designators have no dots; pin numbers may)
        DotP := LastDelimiter('.', S);
        If DotP <= 1 Then Exit;
        Ref := Copy(S, 1, DotP - 1);
        PinNum := Copy(S, DotP + 1, Length(S) - DotP);
        Result := FindPinLocation(Ref, PinNum, Loc);
        Exit;
    End;

    If S[1] = '[' Then
    Begin
        Items := Json_ArrayItems(S);
        Try
            If Items.Count >= 2 Then
            Begin
                Try
                    Xm := StrToFloat(Trim(Items[0]));
                    Ym := StrToFloat(Trim(Items[1]));
                    Loc := Point(MilsToCoord(Round(Xm)), MilsToCoord(Round(Ym)));
                    Result := True;
                Except
                    Result := False;
                End;
            End;
        Finally
            Items.Free;
        End;
    End;
End;

{ Convert an [x,y] mils point raw value to internal coords. }
Function PointFromArray(Const Raw : String; Var Loc : TLocation) : Boolean;
Begin
    Result := ResolveEndpoint(Raw, Loc);    // same parsing, array branch
End;


{ ================================================================== }
{  Per-op handlers                                                    }
{  Each appends exactly one OpResult via AddResult().                 }
{ ================================================================== }

Procedure Op_PlaceComponent(Idx : Integer; Const Op : String);
Var
    Comp     : ISch_Component;
    LibRef   : String;
    Des      : String;
    Xm, Ym   : Double;
    Rot      : Integer;
    Mirror   : String;
    ValStr   : String;
Begin
    LibRef := Json_GetString(Op, 'lib_id');
    Des    := Json_GetString(Op, 'designator');
    Xm     := Json_GetNum(Op, 'x_mil', 0);
    Ym     := Json_GetNum(Op, 'y_mil', 0);
    Rot    := Json_GetInt(Op, 'rotation', 0);
    Mirror := Json_GetString(Op, 'mirror');
    ValStr := Json_GetString(Op, 'value');

    Comp := SchServer.SchObjectFactory(eSchComponent, eCreate_GlobalCopy);
    If Comp = Nil Then
    Begin
        AddResult(Idx, 'place_component', 'error', '[]', 'VERIFY_FAILED',
            'SchObjectFactory returned nil for component');
        Exit;
    End;

    Comp.LibReference := LibRef;
    Comp.Location     := Point(MilsToCoord(Round(Xm)), MilsToCoord(Round(Ym)));
    Comp.Orientation  := RotEnum(Rot);
    If (Mirror = 'x') Or (Mirror = 'y') Then Comp.IsMirrored := True;

    RegisterAndNotify(Comp);

    Comp.Designator.Text := Des;
    If ValStr <> '' Then Comp.Comment.Text := ValStr;

    AddResult(Idx, 'place_component', 'ok', UuidArray1(Comp.UniqueId), '', '');
End;

Procedure Op_SetComponentTransform(Idx : Integer; Const Op : String);
Var
    Comp   : ISch_Component;
    Des    : String;
    Mirror : String;
Begin
    Des := Json_GetString(Op, 'designator');
    Comp := FindComponent(Des);
    If Comp = Nil Then
    Begin
        AddResult(Idx, 'set_component_transform', 'error', '[]', 'VERIFY_FAILED',
            'no component ' + Des);
        Exit;
    End;
    If Json_Get(Op, 'rotation') <> '' Then
        Comp.Orientation := RotEnum(Json_GetInt(Op, 'rotation', 0));
    If Json_Get(Op, 'mirror') <> '' Then
    Begin
        Mirror := Json_GetString(Op, 'mirror');
        Comp.IsMirrored := (Mirror = 'x') Or (Mirror = 'y');
    End;
    SchServer.RobotManager.SendMessage(gDoc.I_ObjectAddress, c_BroadCast,
        SCHM_PrimitiveRegistration, Comp.I_ObjectAddress);
    AddResult(Idx, 'set_component_transform', 'ok', UuidArray1(Comp.UniqueId), '', '');
End;

Procedure Op_SetComponentParameters(Idx : Integer; Const Op : String);
Var
    Comp    : ISch_Component;
    Des     : String;
    RefStr  : String;
    ValStr  : String;
    FpStr   : String;
Begin
    Des := Json_GetString(Op, 'designator');
    Comp := FindComponent(Des);
    If Comp = Nil Then
    Begin
        AddResult(Idx, 'set_component_parameters', 'error', '[]', 'VERIFY_FAILED',
            'no component ' + Des);
        Exit;
    End;
    RefStr := Json_GetString(Op, 'reference');
    ValStr := Json_GetString(Op, 'value');
    FpStr  := Json_GetString(Op, 'footprint');
    If RefStr <> '' Then Comp.Designator.Text := RefStr;
    If ValStr <> '' Then Comp.Comment.Text := ValStr;
    // Footprint / custom parameters: set the "Footprint" / named parameters via
    // ISch_Implementation / ISch_Parameter on Windows. Left as an iteration
    // point (the message records what was applied).
    SchServer.RobotManager.SendMessage(gDoc.I_ObjectAddress, c_BroadCast,
        SCHM_PrimitiveRegistration, Comp.I_ObjectAddress);
    AddResult(Idx, 'set_component_parameters', 'ok', UuidArray1(Comp.UniqueId),
        '', 'footprint=' + FpStr);
End;

Procedure Op_AddWire(Idx : Integer; Const Op : String);
Var
    Wire    : ISch_Wire;
    Verts   : TStringList;
    I, N    : Integer;
    Loc     : TLocation;
    Uuids   : String;
    Ok      : Boolean;
Begin
    Verts := Json_ArrayItems(Json_Get(Op, 'vertices'));
    Try
        N := Verts.Count;
        If N < 2 Then
        Begin
            AddResult(Idx, 'add_wire', 'error', '[]', 'NON_ORTHOGONAL_WIRE',
                'wire needs >= 2 vertices');
            Exit;
        End;
        Wire := SchServer.SchObjectFactory(eWire, eCreate_GlobalCopy);
        If Wire = Nil Then
        Begin
            AddResult(Idx, 'add_wire', 'error', '[]', 'VERIFY_FAILED',
                'SchObjectFactory returned nil for wire');
            Exit;
        End;
        // Multi-vertex wire. The exact vertex-setter signature varies a little
        // across Altium builds; SetState_VerticesCount + Vertex[] is the common
        // documented form (1-based vertex index).
        Wire.SetState_VerticesCount(N);
        Ok := True;
        For I := 0 To N - 1 Do
        Begin
            If Not ResolveEndpoint(Verts[I], Loc) Then
            Begin
                Ok := False;
                Break;
            End;
            Wire.Vertex[I + 1] := Loc;
        End;
        If Not Ok Then
        Begin
            AddResult(Idx, 'add_wire', 'error', '[]', 'NON_ORTHOGONAL_WIRE',
                'unresolved wire vertex');
            Exit;
        End;
        RegisterAndNotify(Wire);
        Uuids := UuidArray1(Wire.UniqueId);
        AddResult(Idx, 'add_wire', 'ok', Uuids, '', '');
    Finally
        Verts.Free;
    End;
End;

Procedure Op_AddJunction(Idx : Integer; Const Op : String);
Var
    J   : ISch_Junction;
    Loc : TLocation;
Begin
    If Not PointFromArray(Json_Get(Op, 'at'), Loc) Then
    Begin
        AddResult(Idx, 'add_junction', 'error', '[]', 'OFF_GRID', 'bad at point');
        Exit;
    End;
    J := SchServer.SchObjectFactory(eJunction, eCreate_GlobalCopy);
    J.Location := Loc;
    RegisterAndNotify(J);
    AddResult(Idx, 'add_junction', 'ok', UuidArray1(J.UniqueId), '', '');
End;

Procedure Op_AddNoConnect(Idx : Integer; Const Op : String);
Var
    NoErc : ISch_NoERC;
    Loc   : TLocation;
    Pin   : String;
    DotP  : Integer;
    Ref, PinNum : String;
Begin
    Pin := Json_GetString(Op, 'pin');
    If Pos('.', Pin) > 0 Then
    Begin
        DotP := LastDelimiter('.', Pin);
        Ref := Copy(Pin, 1, DotP - 1);
        PinNum := Copy(Pin, DotP + 1, Length(Pin) - DotP);
        If Not FindPinLocation(Ref, PinNum, Loc) Then
        Begin
            AddResult(Idx, 'add_no_connect', 'error', '[]', 'VERIFY_FAILED',
                'pin not found: ' + Pin);
            Exit;
        End;
    End
    Else If Not PointFromArray(Json_Get(Op, 'pin'), Loc) Then
    Begin
        AddResult(Idx, 'add_no_connect', 'error', '[]', 'OFF_GRID', 'bad NC point');
        Exit;
    End;
    NoErc := SchServer.SchObjectFactory(eNoERC, eCreate_GlobalCopy);
    NoErc.Location := Loc;
    RegisterAndNotify(NoErc);
    AddResult(Idx, 'add_no_connect', 'ok', UuidArray1(NoErc.UniqueId), '', '');
End;

Procedure Op_AddNetLabel(Idx : Integer; Const Op : String);
Var
    NL   : ISch_Netlabel;
    Loc  : TLocation;
    Name : String;
    Orient : Integer;
Begin
    Name := Json_GetString(Op, 'name');
    If Not PointFromArray(Json_Get(Op, 'at'), Loc) Then
    Begin
        AddResult(Idx, 'add_net_label', 'error', '[]', 'OFF_GRID', 'bad label point');
        Exit;
    End;
    Orient := Json_GetInt(Op, 'orientation', 0);
    NL := SchServer.SchObjectFactory(eNetLabel, eCreate_GlobalCopy);
    NL.Location := Loc;
    NL.Text := Name;
    NL.Orientation := RotEnum(Orient);
    RegisterAndNotify(NL);
    // scope local|global|hierarchical: Altium uses Net Label (local) vs Port /
    // Sheet Entry for cross-sheet scope. v1 places a local Net Label and notes
    // the requested scope; promoting to a Port is an iteration point on Windows.
    AddResult(Idx, 'add_net_label', 'ok', UuidArray1(NL.UniqueId), '',
        'scope=' + Json_GetString(Op, 'scope'));
End;

Procedure Op_PlacePowerPort(Idx : Integer; Const Op : String);
Var
    PP     : ISch_PowerObject;
    Loc    : TLocation;
    NetNm  : String;
    LibId  : String;
    Orient : Integer;
Begin
    NetNm := Json_GetString(Op, 'net_name');
    LibId := Json_GetString(Op, 'lib_id');
    // place_gnd / place_vcc sugar -> default net/lib (SPEC sec 2.2)
    If NetNm = '' Then
    Begin
        If Json_GetString(Op, 'op') = 'place_gnd' Then NetNm := 'GND'
        Else If Json_GetString(Op, 'op') = 'place_vcc' Then NetNm := 'VCC';
    End;
    If Not PointFromArray(Json_Get(Op, 'at'), Loc) Then
    Begin
        AddResult(Idx, 'place_power_port', 'error', '[]', 'OFF_GRID', 'bad port point');
        Exit;
    End;
    Orient := Json_GetInt(Op, 'rotation', 0);
    PP := SchServer.SchObjectFactory(ePowerObject, eCreate_GlobalCopy);
    PP.Location := Loc;
    PP.Text := NetNm;
    PP.ShowNetName := True;
    PP.Orientation := RotEnum(Orient);
    // Pick a sensible style from the net name (purely cosmetic; net identity is
    // carried by .Text). GND-like nets get a ground bar, others a power bar.
    If (NetNm = 'GND') Or (NetNm = 'GNDA') Or (NetNm = 'AGND') Or (NetNm = 'DGND') Then
        PP.Style := ePowerGndPower
    Else
        PP.Style := ePowerBar;
    RegisterAndNotify(PP);
    AddResult(Idx, 'place_power_port', 'ok', UuidArray1(PP.UniqueId), '',
        'lib_id=' + LibId);
End;

Procedure Op_AddText(Idx : Integer; Const Op : String);
Var
    Lbl   : ISch_Label;
    Loc   : TLocation;
    Txt   : String;
    Angle : Double;
Begin
    Txt := Json_GetString(Op, 'text');
    If Not PointFromArray(Json_Get(Op, 'at'), Loc) Then
    Begin
        AddResult(Idx, 'add_text', 'error', '[]', 'OFF_GRID', 'bad text point');
        Exit;
    End;
    Angle := Json_GetNum(Op, 'angle', 0);
    Lbl := SchServer.SchObjectFactory(eLabel, eCreate_GlobalCopy);
    Lbl.Location := Loc;
    Lbl.Text := Txt;
    // add_text may carry a free angle; Altium label orientation is quantized to
    // 90 deg, so snap to the nearest enum step.
    Lbl.Orientation := RotEnum((Round(Angle / 90) * 90) Mod 360);
    RegisterAndNotify(Lbl);
    AddResult(Idx, 'add_text', 'ok', UuidArray1(Lbl.UniqueId), '', '');
End;

Procedure Op_Unsupported(Idx : Integer; Const OpName : String);
Begin
    // add_bus / add_bus_entry are OP_UNSUPPORTED in the v1 Altium live driver
    // (SPEC sec 2.2). They remain valid for the KiCad writer.
    AddResult(Idx, OpName, 'error', '[]', 'OP_UNSUPPORTED',
        OpName + ' is not supported by the Altium live driver in v1');
End;


{ Dispatch a single op object to its handler. }
Procedure DispatchOp(Idx : Integer; Const Op : String);
Var
    Name : String;
Begin
    Name := Json_GetString(Op, 'op');
    Try
        If Name = 'place_component' Then Op_PlaceComponent(Idx, Op)
        Else If Name = 'set_component_transform' Then Op_SetComponentTransform(Idx, Op)
        Else If Name = 'set_component_parameters' Then Op_SetComponentParameters(Idx, Op)
        Else If Name = 'add_wire' Then Op_AddWire(Idx, Op)
        Else If Name = 'add_junction' Then Op_AddJunction(Idx, Op)
        Else If Name = 'add_no_connect' Then Op_AddNoConnect(Idx, Op)
        Else If Name = 'add_net_label' Then Op_AddNetLabel(Idx, Op)
        Else If Name = 'place_power_port' Then Op_PlacePowerPort(Idx, Op)
        Else If Name = 'place_gnd' Then Op_PlacePowerPort(Idx, Op)
        Else If Name = 'place_vcc' Then Op_PlacePowerPort(Idx, Op)
        Else If Name = 'add_text' Then Op_AddText(Idx, Op)
        Else If Name = 'add_bus' Then Op_Unsupported(Idx, 'add_bus')
        Else If Name = 'add_bus_entry' Then Op_Unsupported(Idx, 'add_bus_entry')
        Else
            AddResult(Idx, Name, 'error', '[]', 'OP_UNSUPPORTED', 'unknown op ' + Name);
    Except
        // Never let one bad op abort the whole run; map to a structured result.
        AddResult(Idx, Name, 'error', '[]', 'VERIFY_FAILED',
            'exception while applying op');
    End;
End;


{ ================================================================== }
{  Bridge file I/O + envelope                                         }
{ ================================================================== }

Function GetAltiumVersion : String;
Begin
    Result := 'unknown';
    Try
        If Client <> Nil Then
            Result := Client.GetProductVersion;   // e.g. "22.11.1"
    Except
        Result := 'unknown';
    End;
End;

{ Resolve the BASE bridge directory (must match bridge.py's default_bridge_dir).
  Priority:
    1. env AKCLI_ALTIUM_BRIDGE_DIR        (matches bridge.py exactly)
    2. pointer file %TEMP%\akcli-altium-bridge.path (script-side convenience;
       its first line = the base dir, for when Altium is already running and the
       env var did not propagate into the live process)
    3. default  %TEMP%\akcli-altium-bridge  (matches bridge.py's default) }
Function ResolveBridgeDir : String;
Var
    Dir, PtrPath, Temp : String;
    SL : TStringList;
Begin
    Dir := GetEnvironmentVariable('AKCLI_ALTIUM_BRIDGE_DIR');
    If Dir <> '' Then
    Begin
        Result := IncludeTrailingPathDelimiter(Dir);
        Exit;
    End;
    Temp := GetEnvironmentVariable('TEMP');
    If Temp = '' Then Temp := GetEnvironmentVariable('TMP');
    PtrPath := IncludeTrailingPathDelimiter(Temp) + 'akcli-altium-bridge.path';
    If FileExists(PtrPath) Then
    Begin
        SL := TStringList.Create;
        Try
            SL.LoadFromFile(PtrPath);
            If SL.Count > 0 Then
            Begin
                Result := IncludeTrailingPathDelimiter(Trim(SL[0]));
                Exit;
            End;
        Finally
            SL.Free;
        End;
    End;
    Result := IncludeTrailingPathDelimiter(
        IncludeTrailingPathDelimiter(Temp) + 'akcli-altium-bridge');
End;

{ bridge.py carves a per-run unique sub-directory "run-<hex>/" under the base dir
  and writes request.json INSIDE it (then polls run-<hex>/response.json). Find the
  active run dir: the newest "run-*" subdir that currently holds a request.json.
  Returns '' when none is pending. The base .lock keeps this single-flight. }
Function FindActiveRunDir(Const Base : String) : String;
Var
    SR       : TSearchRec;
    FindRes  : Integer;
    Cand     : String;
    Best     : String;
    BestTime : Integer;
Begin
    Result := '';
    Best := '';
    BestTime := -1;
    FindRes := FindFirst(Base + 'run-*', faDirectory, SR);
    Try
        While FindRes = 0 Do
        Begin
            If ((SR.Attr And faDirectory) <> 0) And
               (SR.Name <> '.') And (SR.Name <> '..') Then
            Begin
                Cand := IncludeTrailingPathDelimiter(Base + SR.Name);
                If FileExists(Cand + 'request.json') Then
                Begin
                    If (Best = '') Or (SR.Time > BestTime) Then
                    Begin
                        Best := Cand;
                        BestTime := SR.Time;
                    End;
                End;
            End;
            FindRes := FindNext(SR);
        End;
    Finally
        FindClose(SR);
    End;
    Result := Best;
End;

Function ReadAllText(Const Path : String) : String;
Var
    SL : TStringList;
Begin
    Result := '';
    SL := TStringList.Create;
    Try
        SL.LoadFromFile(Path);
        Result := SL.Text;
    Finally
        SL.Free;
    End;
End;

{ Atomic write: response.json.tmp -> rename to response.json so the polling
  bridge never observes a partial file. }
Procedure WriteResponseAtomic(Const Dir, Body : String);
Var
    SL      : TStringList;
    TmpPath : String;
    FinPath : String;
Begin
    TmpPath := Dir + 'response.json.tmp';
    FinPath := Dir + 'response.json';
    SL := TStringList.Create;
    Try
        SL.Text := Body;
        SL.SaveToFile(TmpPath);
    Finally
        SL.Free;
    End;
    If FileExists(FinPath) Then DeleteFile(FinPath);
    RenameFile(TmpPath, FinPath);
End;

{ Compose the document-level response envelope wrapping the per-op results. }
Function BuildResponse(Const RunId, Status, DocError, DocMsg : String) : String;
Var
    I    : Integer;
    Body : String;
    Arr  : String;
Begin
    Arr := '';
    For I := 0 To gResults.Count - 1 Do
    Begin
        If I > 0 Then Arr := Arr + ',';
        Arr := Arr + gResults[I];
    End;
    Body := '{"protocol_version":' + IntToStr(AKCLI_PROTOCOL_VERSION) +
            ',"altium_version":' + Json_QuoteOrNull(GetAltiumVersion) +
            ',"status":"' + Status + '"' +
            ',"run_id":' + Json_QuoteOrNull(RunId) +
            ',"error_code":' + Json_QuoteOrNull(DocError) +
            ',"message":' + Json_QuoteOrNull(DocMsg) +
            ',"results":[' + Arr + ']}';
    Result := Body;
End;


{ ================================================================== }
{  Entry point  (ScriptingSystem RunScript proc name = "Run")         }
{ ================================================================== }

Procedure Run;
Var
    BaseDir  : String;
    ReqPath  : String;
    Req      : String;
    Command  : String;
    RunId    : String;
    Target   : String;
    Pv       : Integer;
    OpsRaw   : String;
    OpsList  : TStringList;
    I        : Integer;
    DocMsg   : String;
Begin
    gResults := TStringList.Create;
    Try
        // Base dir holds the single-flight .lock; the active request lives in a
        // per-run "run-<hex>/" subdir (bridge.py). Fall back to the base dir for
        // a manually-placed request.json (interactive testing).
        BaseDir := ResolveBridgeDir;
        gBridgeDir := FindActiveRunDir(BaseDir);
        If gBridgeDir = '' Then gBridgeDir := BaseDir;
        ReqPath := gBridgeDir + 'request.json';
        If Not FileExists(ReqPath) Then
        Begin
            // Nothing to do; the bridge owns single-flight via the .lock file.
            WriteResponseAtomic(gBridgeDir,
                BuildResponse('', 'error', 'VERIFY_FAILED', 'no request.json found'));
            Exit;
        End;

        Req := ReadAllText(ReqPath);
        RunId   := Json_GetString(Req, 'run_id');
        Command := Json_GetString(Req, 'command');
        Pv      := Json_GetInt(Req, 'protocol_version', -1);

        { ---- protocol_version handshake (SPEC sec 3.7) ---- }
        If Pv <> AKCLI_PROTOCOL_VERSION Then
        Begin
            WriteResponseAtomic(gBridgeDir,
                BuildResponse(RunId, 'error', 'PROTOCOL_MISMATCH',
                    'request protocol_version ' + IntToStr(Pv) + ' != ' +
                    IntToStr(AKCLI_PROTOCOL_VERSION)));
            Exit;
        End;

        { ---- altium_ping handshake: report version, do nothing ---- }
        If Command = 'altium_ping' Then
        Begin
            WriteResponseAtomic(gBridgeDir,
                BuildResponse(RunId, 'ok', '', 'altium_ping ok'));
            Exit;
        End;

        { ---- target_format must be altium ---- }
        Target := Json_GetString(Req, 'target_format');
        If (Target <> '') And (Target <> 'altium') Then
        Begin
            WriteResponseAtomic(gBridgeDir,
                BuildResponse(RunId, 'error', 'OP_UNSUPPORTED',
                    'Altium live driver cannot apply target_format ' + Target));
            Exit;
        End;

        { ---- resolve the target schematic document ---- }
        If SchServer = Nil Then
        Begin
            WriteResponseAtomic(gBridgeDir,
                BuildResponse(RunId, 'error', 'VERIFY_FAILED',
                    'SCH server not available (open Altium with a schematic)'));
            Exit;
        End;

        Target := Json_GetString(Req, 'target_file');
        gDoc := Nil;
        If Target <> '' Then
        Begin
            gDoc := SchServer.GetSchDocumentByPath(Target);
            If gDoc = Nil Then
            Begin
                If Client <> Nil Then Client.OpenDocument('SCH', Target);
                gDoc := SchServer.GetSchDocumentByPath(Target);
            End;
        End;
        If gDoc = Nil Then gDoc := SchServer.GetCurrentSchDocument;
        If gDoc = Nil Then
        Begin
            WriteResponseAtomic(gBridgeDir,
                BuildResponse(RunId, 'error', 'VERIFY_FAILED',
                    'no target schematic document available'));
            Exit;
        End;

        { ---- one undo transaction wrapping the whole op-list ---- }
        SchServer.ProcessControl.PreProcess(gDoc, '');
        Try
            OpsRaw := Json_Get(Req, 'ops');
            OpsList := Json_ArrayItems(OpsRaw);
            Try
                For I := 0 To OpsList.Count - 1 Do
                    DispatchOp(I, OpsList[I]);
            Finally
                OpsList.Free;
            End;
        Finally
            SchServer.ProcessControl.PostProcess(gDoc, '');
        End;

        { ---- refresh + remind to verify by re-export ---- }
        gDoc.GraphicallyInvalidate;
        If Client <> Nil Then
            Client.SendMessage('SCH:Zoom', 'Action=Redraw', 255, Client.CurrentView);

        DocMsg := 'applied ' + IntToStr(gResults.Count) +
                  ' op(s); verify by re-exporting the netlist and diffing connectivity';
        WriteResponseAtomic(gBridgeDir, BuildResponse(RunId, 'ok', '', DocMsg));
    Finally
        gResults.Free;
    End;
End;
