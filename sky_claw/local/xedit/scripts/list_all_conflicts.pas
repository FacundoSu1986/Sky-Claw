{
  list_all_conflicts.pas — xEdit Pascal script for record-level conflict detection.

  Iterates over all loaded plugins and reports every record that appears
  in more than one plugin with real changes (ITM / benign override chains are
  skipped via ConflictAllForMainRecord).  Output goes through AddMessage to
  the xEdit log (-R:<file>; xEdit is a GUI binary and has no stdout) in a
  machine-parseable pipe-delimited format so that ConflictAnalyzer can
  consume it.

  Output format (one line per conflict):
    CONFLICT|<FormID>|<EditorID>|<RecordType>|<WinnerPlugin>|<LoserPlugin1>,<LoserPlugin2>

  Per-version critical flag state (T-19a; one line per record version —
  master and each override — only for signatures with critical flags):
    FLAG|<FormID>|<Plugin>|<FlagName>|<0/1>

  ('|' es separador seguro: invalido en filenames de Windows, a diferencia
  de ','/':' que aparecen en plugins reales como "Bashed Patch, 0.esp".)

  Final summary line:
    SUMMARY|total_conflicts=<N>|critical=<N>|minor=<N>

  This script is READ-ONLY — it never modifies any loaded plugin.
}
unit list_all_conflicts;

var
  totalConflicts, criticalCount, minorCount: Integer;

{ Mantener sincronizado con DEFAULT_CRITICAL_TYPES / DEFAULT_WARNING_TYPES
  de conflict_analyzer.py — anclado por tests/test_conflict_signatures_sync.py.
  SCA-001/T-08: la firma de scripts de Oblivion no existe en Skyrim SE; los
  tipos relevantes son SCEN e INFO. }
function IsCriticalType(sig: string): Boolean;
begin
  Result := (sig = 'NPC_') or (sig = 'QUST') or (sig = 'SCEN') or
            (sig = 'INFO') or (sig = 'PERK') or (sig = 'SPEL') or
            (sig = 'MGEF') or (sig = 'FACT') or (sig = 'DIAL') or
            (sig = 'PACK');
end;

function IsWarningType(sig: string): Boolean;
begin
  Result := (sig = 'CELL') or (sig = 'WRLD') or (sig = 'REFR') or
            (sig = 'ACHR') or (sig = 'NAVM') or (sig = 'LAND') or
            (sig = 'WEAP') or (sig = 'ARMO') or (sig = 'AMMO') or
            (sig = 'BOOK') or (sig = 'INGR') or (sig = 'ALCH') or
            (sig = 'MISC') or (sig = 'CONT') or (sig = 'DOOR') or
            (sig = 'LIGH') or (sig = 'STAT') or (sig = 'FLOR') or
            (sig = 'FURN') or (sig = 'LVLI') or (sig = 'LVLN') or
            (sig = 'LVSP') or (sig = 'ENCH') or (sig = 'OTFT') or
            (sig = 'RACE') or (sig = 'COBJ') or (sig = 'KYWD');
end;

{ T-19a: estado del flag "Manual Cost Calc" (bit $1 de SPIT\Flags) para UNA
  version del record SPEL. Mantener sincronizado con CRITICAL_FLAGS de
  conflict_analyzer.py — anclado por tests/test_conflict_signatures_sync.py.
  Honestidad: sin subrecord SPIT no se emite nada (ausencia = "desconocido",
  no "flag apagado"). }
procedure EmitSpellFlagState(rec: IInterface; fid: string);
var
  flagValue: string;
begin
  { El path completo: un SPIT presente pero sin campo Flags no debe tumbar
    el script ni emitir un 0 inventado (review Copilot #259). }
  if not ElementExists(rec, 'SPIT\Flags') then
    Exit;
  if (GetElementNativeValues(rec, 'SPIT\Flags') and $1) <> 0 then
    flagValue := '1'
  else
    flagValue := '0';
  AddMessage('FLAG|' + fid + '|' + GetFileName(GetFile(rec)) + '|Manual Cost Calc|' + flagValue);
end;

{ Emite el estado del flag para el master y cada override del SPEL. }
procedure EmitSpellFlagStates(e: IInterface; fid: string);
var
  i: Integer;
begin
  EmitSpellFlagState(e, fid);
  for i := 0 to OverrideCount(e) - 1 do
    EmitSpellFlagState(OverrideByIndex(e, i), fid);
end;

function Initialize: Integer;
begin
  totalConflicts := 0;
  criticalCount := 0;
  minorCount := 0;
  Result := 0;
end;

function Process(e: IInterface): Integer;
var
  i: Integer;
  sig, fid, edid, winner: string;
  masterRec, overrideRec: IInterface;
  nOverrides: Integer;
  loserList: string;
begin
  Result := 0;

  { Only process records that have overrides.
    Los nombres de las locales NO pueden coincidir con funciones de xEdit
    (FormID, EditorID, OverrideCount...): Pascal no distingue mayusculas y la
    local oculta a la funcion (anclado por tests/test_xedit_headless_contract.py). }
  nOverrides := OverrideCount(e);
  if nOverrides < 1 then
    Exit;

  { We only want to process the master record, not overrides themselves. }
  if not IsMaster(e) then
    Exit;

  { Descartar cadenas ITM / benignas (caNoConflict, caConflictBenign): no son
    disputas entre mods. Mismo filtro que el script oficial de xEdit
    "Detect conflict between elements.pas". }
  if ConflictAllForMainRecord(e) < caOverride then
    Exit;

  sig := Signature(e);
  fid := IntToHex(FormID(e), 8);
  edid := EditorID(e);

  { The winning record is the last override in load order. }
  masterRec := e;
  winner := GetFileName(GetFile(WinningOverride(e)));
  loserList := '';

  { Collect all losing plugins (everyone except the winner). }
  for i := 0 to nOverrides - 1 do begin
    overrideRec := OverrideByIndex(e, i);
    if GetFileName(GetFile(overrideRec)) <> winner then begin
      if loserList <> '' then
        loserList := loserList + ',';
      loserList := loserList + GetFileName(GetFile(overrideRec));
    end;
  end;

  { Also add the master file as a loser if it is not the winner. }
  if GetFileName(GetFile(masterRec)) <> winner then begin
    if loserList <> '' then
      loserList := loserList + ',';
    loserList := loserList + GetFileName(GetFile(masterRec));
  end;

  { Skip if no losers (record only exists in one plugin after all). }
  if loserList = '' then
    Exit;

  { Output the conflict line. }
  AddMessage('CONFLICT|' + fid + '|' + edid + '|' + sig + '|' + winner + '|' + loserList);

  { T-19a: estado de flags criticos por version (solo firmas con flags). }
  if sig = 'SPEL' then
    EmitSpellFlagStates(e, fid);

  Inc(totalConflicts);
  if IsCriticalType(sig) then
    Inc(criticalCount)
  else if not IsWarningType(sig) then
    Inc(minorCount);
end;

function Finalize: Integer;
var
  warningCount: Integer;
begin
  warningCount := totalConflicts - criticalCount - minorCount;
  AddMessage('SUMMARY|total_conflicts=' + IntToStr(totalConflicts) +
             '|critical=' + IntToStr(criticalCount) +
             '|minor=' + IntToStr(minorCount));
  Result := 0;
end;

end.
