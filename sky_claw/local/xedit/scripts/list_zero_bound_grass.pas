{
  list_zero_bound_grass.pas — xEdit Pascal script: records GRAS con Object
  Bounds nulos o ausentes.

  Deteccion del fallo silencioso del precache de NGIO (Stage 8 del SOP): el
  raycasting no puede computar volumen cero, asi que un GRAS con OBND en
  (0,0,0,0,0,0) se descarta por completo y el cache sale vacio o huerfano sin
  ningun error. La remediacion es manual (Recalc Bounds en Creation Kit sobre
  el mod de origen) — este script solo diagnostica.

  Se evalua la version GANADORA de cada GRAS (lo que NGIO ve): un parche que
  arregla los bounds mas abajo en el load order resuelve el problema aunque el
  master original siga roto.

  Output (stdout via AddMessage, pipe-delimited; consumido por
  grass_analyzer.py, anclado por tests/test_grass_scripts_sync.py):

    ZEROBOUND|<FormID 8hex>|<EditorID>|<WinnerPlugin>|<OriginPlugin>|<reason>

  reason: 'zeros' = OBND presente todo-ceros; 'missing' = subrecord OBND
  ausente. Son dos fallos distintos — la ausencia no se inventa como cero
  (honestidad tipo SPIT, review Copilot #259).

  WinnerPlugin = la version GANADORA, la que NGIO ve rota: es el mod a
  purgar/arreglar (Recalc Bounds en Creation Kit) segun el SOP §2.8.
  OriginPlugin = el master que introdujo el record originalmente — solo
  contexto. Si un override tardio rompe un record cuyo master tenia bounds
  validos, OriginPlugin sigue siendo inocente: NO usarlo como el culpable.

  Linea final (obligatoria — su ausencia = scan truncado):

    SUMMARY|total_gras=<N>|zero_bounds=<M>

  Este script es READ-ONLY — jamas modifica un plugin cargado.
}
unit list_zero_bound_grass;

var
  totalGras, zeroCount: Integer;

function Initialize: Integer;
begin
  totalGras := 0;
  zeroCount := 0;
  Result := 0;
end;

{ ¿Los seis campos del OBND de la version *win* son cero? }
function BoundsAllZero(win: IInterface): Boolean;
begin
  Result := (GetElementNativeValues(win, 'OBND\X1') = 0) and
            (GetElementNativeValues(win, 'OBND\Y1') = 0) and
            (GetElementNativeValues(win, 'OBND\Z1') = 0) and
            (GetElementNativeValues(win, 'OBND\X2') = 0) and
            (GetElementNativeValues(win, 'OBND\Y2') = 0) and
            (GetElementNativeValues(win, 'OBND\Z2') = 0);
end;

function Process(e: IInterface): Integer;
var
  win: IInterface;
  reason: string;
begin
  Result := 0;
  if Signature(e) <> 'GRAS' then
    Exit;
  { Una vez por record logico (mismo criterio que list_all_conflicts.pas). }
  if not IsMaster(e) then
    Exit;
  Inc(totalGras);

  win := WinningOverride(e);
  if not ElementExists(win, 'OBND') then
    reason := 'missing'
  else if BoundsAllZero(win) then
    reason := 'zeros'
  else
    Exit;

  AddMessage('ZEROBOUND|' + IntToHex(FormID(e), 8) + '|' + EditorID(win) + '|' +
             GetFileName(GetFile(win)) + '|' + GetFileName(GetFile(e)) + '|' + reason);
  Inc(zeroCount);
end;

function Finalize: Integer;
begin
  AddMessage('SUMMARY|total_gras=' + IntToStr(totalGras) +
             '|zero_bounds=' + IntToStr(zeroCount));
  Result := 0;
end;

end.
