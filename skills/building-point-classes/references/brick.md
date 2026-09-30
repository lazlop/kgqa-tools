# Brick reference for point mapping

Checked against Brick 1.4.4. Namespaces: `brick:` is `https://brickschema.org/schema/Brick#`,
and `tag:` is `https://brickschema.org/schema/BrickTag#`.

Contents:
1. Pick the point function first
2. How class names are built
3. What a class carries: quantity, substance, tags, definition
4. Deprecated classes
5. How specific to be
6. Writing an extension
7. Brick and 223P side by side

## 1. Pick the point function first

Every point class descends from one of the six children of `brick:Point`. Decide which one
before looking for the specific class. That settles most ambiguity, and it also keeps you out
of the huge `Point` subtree (937 descendants).

| Function | Descendants | The point is... | Typical BACnet |
|---|---|---|---|
| `Sensor` | 300 | a measurement or calculated reading | AI, read-only AV |
| `Setpoint` | 257 | a target the control loop drives toward | commandable AV |
| `Parameter` | 148 | a configuration value: delays, gains, limits, deadbands, multipliers | commandable AV/MV |
| `Status` | 88 | a reported state | BI, read-only BV/MV |
| `Command` | 74 | an instruction to equipment: start/stop, enable, position output | BO, AO, commandable BV |
| `Alarm` | 64 | an alarm condition | BV, event enrollment |

Controller exports blur these. Heuristics that held up on a G36 VAV point list:

- **"… Mode" as BV:**
  - it's a `Status` when the controller reports it ("Test Mode", "Bypass Mode");
  - it's a `Command` when an operator sets it ("Maintenance Mode Enable").
- **"… Enable" or "… Inhibit":** `Command` if it switches a function on or off
  (`Enable_Command` and its subclasses), `Parameter` if it's a stored configuration choice.
- **Limits and delays** (minimum flow, time delays, reset multipliers) are `Parameter`s, even
  when named like setpoints. The exceptions are the true min/max *setpoints* Brick models
  explicitly (`Max_Air_Flow_Setpoint_Limit`, ...). Search for them.
- **Feedback** (position feedback, status feedback) is a `Sensor` or a `Status`, never a
  `Command`.

## 2. How class names are built

Names read as `[qualifiers] [substance] [quantity] [function]`:
- `Discharge_Air_Temperature_Sensor`
- `Occupied_Cooling_Zone_Air_Temperature_Setpoint`
- `Fan_On_Off_Status`

Most classes have several parents (209 classes in 1.4.4). `Fan_On_Off_Status` is both a
`Fan_Status` and an `On_Off_Status`. So "the" parent is rarely unique. Use query 2 to see all
the paths.

Because the names are compositional, keyword search over labels usually works:
`search(dataset, "fan status", kind="class")` ranks `Fan_Status` and `Fan_On_Off_Status`
first. When names in the source data are cryptic, search by quantity (query 6) or by tags
(query 7) instead.

## 3. What a class carries

A typical Brick class has:
- `rdfs:subClassOf`, often several;
- `skos:definition` (not on every class);
- `brick:hasQuantity`: a QUDT quantity kind, for example `qudtqk:Temperature`;
- `brick:hasSubstance`: for example `brick:Zone_Air`;
- `brick:hasAssociatedTag`: Haystack-style tags;
- `sh:rule` blocks: the rules that infer the class from tags. Ignore these when choosing a
  class.

**Coverage is uneven.** In 1.4.4, `hasQuantity` is on almost every current `Sensor` and most
`Setpoint`s, but only on 5 of 88 `Status` subclasses and 8 of 74 `Command` subclasses. A few
current sensors lack it too (`Discharge_Air_Temperature_Sensor`). Query by quantity for
sensors and setpoints; use tags or search for statuses and commands.

## 4. Deprecated classes

About 240 classes are `owl:deprecated true`, most with `brick:isReplacedBy` and a
`brick:deprecationMitigationMessage` explaining the change. Search returns them like any other
class, so check every class you choose (query 8), including parents you fall back to. The biggest group is the
water-side renaming from `Supply`/`Return` to `Leaving`/`Entering`, for example
`Chilled_Water_Supply_Temperature_Sensor` → `Leaving_Chilled_Water_Temperature_Sensor`. Air-side
names such as `Supply_Air_*` and `Discharge_Air_*` are current.

## 5. How specific to be

Choose the most specific class the evidence supports, and no more specific. If a point is named
"Zone Temp" and you know it's a zone air temperature, `Zone_Air_Temperature_Sensor` is right.
If you don't know whether a "Temp Setpoint" is for heating or cooling, use a common parent and
note the ambiguity. Don't pick a sibling.

Fallback parents can be deprecated too. The obvious common parent here,
`Zone_Air_Temperature_Setpoint`, has been deprecated since 1.3.0 "in favor of more explicit
class names to distinguish target and cooling/heating setpoints". Its replacement,
`Target_Zone_Air_Temperature_Setpoint`, means the single target setpoint, not "heating or
cooling". The current common parent of the heating and cooling zone setpoints is
`Air_Temperature_Setpoint`. Read the mitigation message rather than blindly following
`isReplacedBy`.

When no class fits and a parent is too vague to be useful, that's the case for an extension.

## 6. Writing an extension

Subclass the most specific fitting class, and mirror Brick's encoding:

```turtle
ext:Damper_Actuator_Motion_Status a owl:Class, sh:NodeShape ;
    rdfs:subClassOf brick:Status ;
    rdfs:label "Damper Actuator Motion Status" ;
    skos:definition "The motion state of a damper actuator (stopped, running, stalled, ...)" ;
    brick:hasAssociatedTag tag:Damper, tag:Motion, tag:Status, tag:Point .
```

- Name new classes with Brick's grammar, so they read like standard ones and search finds them.
- Only use tags that exist in `tag:` (check with search). Add `hasQuantity` and `hasSubstance`
  when they apply.
- Keep extensions in their own namespace and file, and generate them from a CSV if there are
  many. The VAV example used a CSV with columns for class, parent, label and definition.

## 7. Brick and 223P side by side

When a model uses both, the Brick class is the fine-grained label, and 223P carries the
structure. Keep the Brick class on the 223P property (for example `rdfs:seeAlso brick:X`) so
nothing is lost where 223P has no equivalent.

| Brick function | 223P property class | Usual 223P qualifiers |
|---|---|---|
| `Sensor` | `QuantifiableObservableProperty`, observed by a `Sensor` | quantity kind, unit, `ofMedium`/`ofConstituent` |
| `Setpoint` | `QuantifiableActuatableProperty` | `Aspect-Setpoint`, plus `Maximum`/`Minimum`, roles and modes |
| `Parameter` | `QuantifiableActuatableProperty` (or enumerated for a choice) | `Aspect-Threshold`/`-Maximum`/..., extension aspects such as Delay |
| `Command` | `QuantifiableActuatableProperty` (analog output) or `EnumeratedActuatableProperty` (binary) | kind per the state texts |
| `Status` | `EnumeratedObservableProperty` | `Aspect-OperatingStatus` or `-OperatingMode`, kind per the state texts |
| `Alarm` | `EnumeratedObservableProperty` | `Aspect-Alarm`, an alarm-state kind, linked with `hasAlarmStatus` |

Brick's specificity maps onto 223P aspects plus the owner, not onto 223P classes.
`Occupied_Cooling_Zone_Air_Temperature_Setpoint` becomes a quantifiable actuatable property:
- owned by the zone;
- with quantity kind `Temperature` and `ofMedium Fluid-Air`;
- with aspects `Setpoint`, `Role-Cooling` and an occupied-mode aspect.
