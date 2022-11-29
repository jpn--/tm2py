"""Module for highway network preparation steps.

Creates required attributes and populates input values needed
for highway assignments. The toll values, VDFs, per-class cost
(tolls+operating costs), modes and skim link attributes are calculated.

The following link attributes are used as input:
    - "@capclass": link capclass index
    - "length": standard link length, in miles
    - "@tollbooth": label to separate bridgetolls from valuetolls
    - "@tollseg": toll segment, used to index toll value lookups from the toll file
        (under config.highway.tolls.file_path)
    - "@ft": functional class, used to assign VDFs

The following keys and tables are used from the config:
    highway.tolls.file_path: relative path to input toll file
    highway.tolls.src_vehicle_group_names: names used in tolls file for
        toll class values
    highway.tolls.dst_vehicle_group_names: corresponding names used in
        network attributes toll classes
    highway.tolls.valuetoll_start_tollbooth_code: index to split point bridge tolls
        (< this value) from distance value tolls (>= this value)
    highway.classes: the list of assignment classes, see the notes under
        highway_assign for detailed explanation
    highway.capclass_lookup: the lookup table mapping the link @capclass setting
        to capacity (@capacity), free_flow_speed (@free_flow_speec) and
        critical_speed (used to calculate @ja for akcelik type functions)
    highway.generic_highway_mode_code: unique (with other mode_codes) single
        character used to label entire auto network in Emme
    highway.maz_to_maz.mode_code: unique (with other mode_codes) single
        character used to label MAZ local auto network including connectors

The following link attributes are created (overwritten) and are subsequently used in
the highway assignments.
    - "@flow_XX": link PCE flows per class, where XX is the class name in the config
    - "@maz_flow": Assigned MAZ-to-MAZ flow

The following attributes are calculated:
    - vdf: volume delay function to use
    - "@capacity": total link capacity
    - "@ja": akcelik delay parameter
    - "@hov_length": length with HOV lanes
    - "@toll_length": length with tolls
    - "@bridgetoll_YY": the bridge toll for class subgroup YY
    - "@valuetoll_YY": the "value", non-bridge toll for class subgroup YY
    - "@cost_YY": total cost for class YY
"""

import os
from typing import TYPE_CHECKING, Dict, List, Set

from tm2py.components.component import Component, FileFormatError
from tm2py.emme.manager import EmmeNetwork, EmmeScenario
from tm2py.logger import LogStartEnd

if TYPE_CHECKING:
    from tm2py.controller import RunController


class PrepareNetwork(Component):
    """Highway network preparation."""

    def __init__(self, controller: "RunController"):
        """Constructor for PPrepareNetwork.

        Args:
            controller (RunController): Reference to run controller object.
        """
        super().__init__(controller)
        self.config = self.controller.config.highway

    @LogStartEnd("Prepare network attributes and modes")
    def run(self):
        """Run network preparation step."""
        self.dynamic_toll_change = 0

        for time in self.time_period_names:
            with self.controller.emme_manager.logbook_trace(
                f"prepare for highway assignment {time}"
            ):
                scenario = self.get_emme_scenario(
                    self.controller.config.emme.highway_database_path, time
                )
                if self.controller.iteration == 0 and self.controller._dynamic_toll_iter == 0: # only do this in the 1st iteration, otherwise attribute values will be reset
                    self._create_class_attributes(scenario, time)
                network = scenario.get_network()
                self._set_tolls(network, time)
                self._set_vdf_attributes(network, time)
                self._set_link_modes(network)
                self._calc_link_skim_lengths(network)
                self._calc_link_class_costs(network)
                self._calc_total_flow(network)
                scenario.publish_network(network)

        if self.controller.config.highway.tolls.run_dynamic_toll:
            # accumulate dynamic toll iteration
            if (self.controller._dynamic_toll_iter > 0) and (not self.dynamic_toll_change):
                self.controller._stop_dynamic_toll = True
            if not self.controller._stop_dynamic_toll:
                self.controller._dynamic_toll_iter += 1

    def validate_inputs(self):
        """Validate inputs files are correct, raise if an error is found."""
        toll_file_path = self.get_abs_path(self.config.tolls.file_path)
        if not os.path.exists(toll_file_path):
            self.logger.log(
                f"Tolls file (config.highway.tolls.file_path) does not exist: {toll_file_path}",
                level="ERROR",
            )
            raise FileNotFoundError(f"Tolls file does not exist: {toll_file_path}")
        src_veh_groups = self.config.tolls.src_vehicle_group_names
        columns = ["fac_index"]
        for time in self.controller.config.time_periods:
            for vehicle in src_veh_groups:
                columns.append(f"toll{time.name.lower()}_{vehicle}")
        with open(toll_file_path, "r", encoding="UTF8") as toll_file:
            header = set(h.strip() for h in next(toll_file).split(","))
            missing = []
            for column in columns:
                if column not in header:
                    missing.append(column)
                    self.logger.log(
                        f"Tolls file missing column: {column}", level="ERROR"
                    )
        if missing:
            raise FileFormatError(
                f"Tolls file missing {len(missing)} columns: {', '.join(missing)}"
            )

    def _create_class_attributes(self, scenario: EmmeScenario, time_period: str):
        """Create required network attributes including per-class cost and flow attributes."""
        create_attribute = self.controller.emme_manager.tool(
            "inro.emme.data.extra_attribute.create_extra_attribute"
        )
        attributes = {
            "LINK": [
                ("@capacity", "total link capacity"),
                ("@ja", "akcelik delay parameter"),
                ("@maz_flow", "Assigned MAZ-to-MAZ flow"),
                ("@hov_length", "length with HOV lanes"),
                ("@toll_length", "length with tolls"),
            ]
        }

        if self.controller.config.highway.msa.apply_msa or self.config.tolls.run_dynamic_toll:
            attributes["LINK"].extend([
                ("@total_flow", "total traffic flow"),
                ("@vc", "volume to capacity ratio")
            ])

        if self.controller.config.highway.msa.apply_msa:
            attributes["LINK"].extend([
                    ("@total_flow_avg", "average total traffic flow"),
                ])
            if self.controller.config.highway.msa.write_iteration_flow:
                for iteration in range(1, self.controller.config.run.end_iteration + 1):
                    attributes["LINK"].append((f"@total_flow_{iteration}", f"total traffic flow iter{iteration}"))

        if self.config.tolls.run_dynamic_toll:
            attributes["LINK"].extend([
                ("@update_dynamic_toll", "need to update dynamic toll or not")
            ])

        # toll field attributes by bridge and value and toll definition
        dst_veh_groups = self.config.tolls.dst_vehicle_group_names
        for dst_veh in dst_veh_groups:
            for toll_type in "bridge", "value":
                attributes["LINK"].append(
                    (
                        f"@{toll_type}toll_{dst_veh}",
                        f"{toll_type} toll value for {dst_veh}",
                    )
                )
        # results for link cost and assigned flow
        for assign_class in self.config.classes:
            attributes["LINK"].append(
                (
                    f"@cost_{assign_class.name.lower()}",
                    f'{time_period} {assign_class["description"]} total costs'[:40],
                )
            )
            attributes["LINK"].append(
                (
                    f"@flow_{assign_class.name.lower()}",
                    f'{time_period} {assign_class["description"]} link volume'[:40],
                )
            )

            # attributes for storing averaged volume from previous global iterations
            attributes["LINK"].append(
                (
                    f"@flow_{assign_class.name.lower()}_avg",
                    f'{time_period} {assign_class["description"]} link avg volume'[:40],
                )
            )
            if self.controller.config.highway.msa.write_iteration_flow:
                for iteration in range(1, self.controller.config.run.end_iteration + 1):
                    attributes["LINK"].append(
                        (
                            f"@flow_{assign_class.name.lower()}_{iteration}",
                            f'{time_period} {assign_class["description"]} link volume{iteration}'[:40],
                        )
                    )

        for domain, attrs in attributes.items():
            for name, desc in attrs:
                create_attribute(domain, name, desc, overwrite=True, scenario=scenario)

    def _set_tolls(self, network: EmmeNetwork, time_period: str):
        """Set the tolls in the network from the toll reference file."""
        src_veh_groups = self.config.tolls.src_vehicle_group_names
        dst_veh_groups = self.config.tolls.dst_vehicle_group_names
        valuetoll_start_tollbooth_code = self.config.tolls.valuetoll_start_tollbooth_code
        run_dynamic_toll = self.config.tolls.run_dynamic_toll

        if run_dynamic_toll:
            # if using dynamic tolling method, only read in bridge tolls
            toll_index = self._get_toll_indices(toll_file_path = self.get_abs_path(self.config.tolls.bridetoll_file_path))
            global_iteration = self.controller.iteration
            self.logger.log(f"current global iter: {global_iteration}, dynamic toll iter: {self.controller._dynamic_toll_iter}")

            # set up initial state
            if global_iteration == 0 and self.controller._dynamic_toll_iter == 0:
                for link in network.links():
                    if link["@tollbooth"]:
                        if link["@tollbooth"] < valuetoll_start_tollbooth_code:  # bridge toll is fixed using lookup
                            index = int(
                                link["@tollbooth"] * 1000
                                + link["@tollseg"] * 10
                                + link["@useclass"]
                            )
                            data_row = toll_index.get(index)
                            if data_row is None:
                                self.logger.warn(
                                    f"set tolls failed index lookup {index}, link {link.id}",
                                    indent=True,
                                )
                                continue  # tolls will remain at zero
                            for src_veh, dst_veh in zip(src_veh_groups, dst_veh_groups):
                                link[f"@bridgetoll_{dst_veh}"] = (
                                    float(data_row[f"toll{time_period.lower()}_{src_veh}"]) * 100
                                )
                        else: # initialize valuetoll to 0
                            link["@update_dynamic_toll"] = 1 # flag the link indicates that toll rate should be updated
                            for src_veh, dst_veh in zip(src_veh_groups, dst_veh_groups):
                                link[f"@valuetoll_{dst_veh}"] = 0

            # update valuetoll rates dynamically in later iterations
            else:
                # TODO: consider having different increment value by veh class
                VALUETOLL_INCREMENT = 0.25
                max_dynamic_valuetoll = self.config.tolls.max_dynamic_valuetoll
                for link in network.links():
                    if link["@tollbooth"] > 0 and link["@tollbooth"] >= valuetoll_start_tollbooth_code: # only update tolls for valuetoll links
                        if link["@vc"] > 1 and link["@update_dynamic_toll"] == 1:
                            self.dynamic_toll_change += 1
                            increase_ratio = round(link["@vc"])
                            for src_veh, dst_veh in zip(src_veh_groups, dst_veh_groups): # tollway with a per-mile charge
                                # calculate per-mile charge
                                valuetoll_per_mile = (link[f"@valuetoll_{dst_veh}"] / link.length) / 100
                                # updated valuetoll
                                increased_valuetoll = valuetoll_per_mile + VALUETOLL_INCREMENT * increase_ratio
                                if increased_valuetoll > max_dynamic_valuetoll:
                                    link[f"@valuetoll_{dst_veh}"] = (max_dynamic_valuetoll * link.length * 100)
                                    link["@update_dynamic_toll"] = 0 # set link update_dynamic_toll to 0
                                else:
                                    link[f"@valuetoll_{dst_veh}"] = (increased_valuetoll * link.length * 100)

        else: # if run_dynamic_toll is False
            # use lookup table that contains both bridge & value tolls
            toll_index = self._get_toll_indices(toll_file_path = self.get_abs_path(self.config.tolls.file_path))
            for link in network.links():
                if link["@tollbooth"]:
                    index = int(
                        link["@tollbooth"] * 1000
                        + link["@tollseg"] * 10
                        + link["@useclass"]
                    )
                    data_row = toll_index.get(index)
                    if data_row is None:
                        self.logger.warn(
                            f"set tolls failed index lookup {index}, link {link.id}",
                            indent=True,
                        )
                        continue  # tolls will remain at zero
                    # if index is below tollbooth start index then this is a bridge
                    # (point toll), available for all traffic assignment classes
                    if link["@tollbooth"] < valuetoll_start_tollbooth_code:
                        for src_veh, dst_veh in zip(src_veh_groups, dst_veh_groups):
                            link[f"@bridgetoll_{dst_veh}"] = (
                                float(data_row[f"toll{time_period.lower()}_{src_veh}"]) * 100
                            )
                    else:  # else, this is a tollway with a per-mile charge
                        for src_veh, dst_veh in zip(src_veh_groups, dst_veh_groups):
                            link[f"@valuetoll_{dst_veh}"] = (
                                float(data_row[f"toll{time_period.lower()}_{src_veh}"])
                                * link.length
                                * 100
                            )

    def _get_toll_indices(self, toll_file_path: str) -> Dict[int, Dict[str, str]]:
        """Get the mapping of toll lookup table from the toll reference file."""
        self.logger.debug(f"toll_file_path {toll_file_path}", indent=True)
        tolls = {}
        with open(toll_file_path, "r", encoding="UTF8") as toll_file:
            header = [h.strip() for h in next(toll_file).split(",")]
            for line in toll_file:
                data = dict(zip(header, line.split(",")))
                tolls[int(data["fac_index"])] = data
        return tolls

    def _set_vdf_attributes(self, network: EmmeNetwork, time_period: str):
        """Set capacity, VDF and critical speed on links."""
        capacity_map = {}
        critical_speed_map = {}
        for row in self.config.capclass_lookup:
            if row.get("capacity") is not None:
                capacity_map[row["capclass"]] = row.get("capacity")
            if row.get("critical_speed") is not None:
                critical_speed_map[row["capclass"]] = row.get("critical_speed")
        tp_mapping = {
            tp.name.upper(): tp.highway_capacity_factor
            for tp in self.controller.config.time_periods
        }
        period_capacity_factor = tp_mapping[time_period]
        akcelik_vdfs = [3, 4, 5, 6, 7, 9, 10, 11, 12, 13, 14, 99]
        for link in network.links():
            cap_lanehour = capacity_map[link["@capclass"]]
            link["@capacity"] = cap_lanehour * period_capacity_factor * link["@lanes"]
            link.volume_delay_func = int(link["@ft"])
            # re-mapping links with type 99 to type 7 "local road of minor importance"
            if link.volume_delay_func == 99:
                link.volume_delay_func = 7
            # num_lanes not used directly, but set for reference
            link.num_lanes = max(min(9.9, link["@lanes"]), 1.0)
            if link.volume_delay_func in akcelik_vdfs and link["@free_flow_speed"] > 0:
                dist = link.length
                critical_speed = critical_speed_map[link["@capclass"]]
                t_c = dist / critical_speed
                t_o = dist / link["@free_flow_speed"]
                link["@ja"] = 16 * (t_c - t_o) ** 2

    def _calc_total_flow(self, network: EmmeNetwork):
        for link in network.links():
            link["@total_flow"] = 0
            for assign_class in self.config.classes:
                link["@total_flow"] += link[f"@flow_{assign_class.name.lower()}"]

    def _set_link_modes(self, network: EmmeNetwork):
        """Set the link modes based on the per-class 'excluded_links' set."""
        # first reset link modes (script run more than once)
        # "generic_highway_mode_code" must already be created (in import to Emme script)
        auto_mode = {network.mode(self.config.generic_highway_mode_code)}
        used_modes = {
            network.mode(assign_class.mode_code) for assign_class in self.config.classes
        }
        used_modes.add(network.mode(self.config.maz_to_maz.mode_code))
        for link in network.links():
            link.modes -= used_modes
            if link["@drive_link"]:
                link.modes |= auto_mode
        for mode in used_modes:
            if mode is not None:
                network.delete_mode(mode)

        if self.config.maz_to_maz:
            # Create special access/egress mode for MAZ connectors
            maz_access_mode = network.create_mode(
                "AUX_AUTO", self.config.maz_to_maz.mode_code
            )
            maz_access_mode.description = "MAZ access"
        # create modes from class spec
        # (duplicate mode codes allowed provided the excluded_links is the same)
        mode_excluded_links = {}
        for assign_class in self.config.classes:
            if assign_class.mode_code in mode_excluded_links:
                if (
                    assign_class.excluded_links
                    != mode_excluded_links[assign_class.mode_code]
                ):
                    ex_links1 = mode_excluded_links[assign_class.mode_code]
                    ex_links2 = assign_class.excluded_links
                    raise Exception(
                        f"config error: highway.classes, duplicated mode codes "
                        f"('{assign_class.mode_code}') with different excluded "
                        f"links: {ex_links1} and {ex_links2}"
                    )
                continue
            mode = network.create_mode("AUX_AUTO", assign_class.mode_code)
            mode.description = assign_class.name
            mode_excluded_links[mode.id] = assign_class.excluded_links

        dst_veh_groups = self.config.tolls.dst_vehicle_group_names
        for link in network.links():
            modes = set(m.id for m in link.modes)
            if self.config.run_maz_assignment:
                if link.i_node["@maz_id"] + link.j_node["@maz_id"] > 0:
                    modes.add(maz_access_mode.id)
                    link.modes = modes
                    continue
            if not link["@drive_link"]:
                continue
            exclude_links_map = {
                "is_sr": link["@useclass"] in [2, 3],
                "is_sr2": link["@useclass"] == 2,
                "is_sr3": link["@useclass"] == 3,
                "is_auto_only": link["@useclass"] in [2, 3, 4],
            }
            for dst_veh in dst_veh_groups:
                exclude_links_map[f"is_toll_{dst_veh}"] = (
                    link[f"@valuetoll_{dst_veh}"] > 0
                )
            if self.config.maz_to_maz:
                self._apply_exclusions(
                    self.config.maz_to_maz.excluded_links,
                    maz_access_mode.id,
                    modes,
                    exclude_links_map,
                )
            for assign_class in self.config.classes:
                self._apply_exclusions(
                    assign_class.excluded_links,
                    assign_class.mode_code,
                    modes,
                    exclude_links_map,
                )
            link.modes = modes

    @staticmethod
    def _apply_exclusions(
        excluded_links_criteria: List[str],
        mode_code: str,
        modes_set: Set[str],
        link_values: Dict[str, bool],
    ):
        """Apply the exclusion criteria to set the link modes."""
        for criteria in excluded_links_criteria:
            if link_values[criteria]:
                return
        modes_set.add(mode_code)

    def _calc_link_skim_lengths(self, network: EmmeNetwork):
        """Calculate the length attributes used in the highway skims."""
        valuetoll_start_tollbooth_code = self.config.tolls.valuetoll_start_tollbooth_code
        for link in network.links():
            # distance in hov lanes / facilities
            if 2 <= link["@useclass"] <= 3:
                link["@hov_length"] = link.length
            else:
                link["@hov_length"] = 0
            # distance on non-bridge toll facilities
            if link["@tollbooth"] >= valuetoll_start_tollbooth_code:
                link["@toll_length"] = link.length
            else:
                link["@toll_length"] = 0

    def _calc_link_class_costs(self, network: EmmeNetwork):
        """Calculate the per-class link cost from the tolls and operating costs."""
        for assign_class in self.config.classes:
            cost_attr = f"@cost_{assign_class.name.lower()}"
            op_cost = assign_class["operating_cost_per_mile"]
            toll_factor = assign_class.get("toll_factor")
            if toll_factor is None:
                toll_factor = 1.0
            for link in network.links():
                toll_value = sum(link[toll_attr] for toll_attr in assign_class["toll"])
                link[cost_attr] = link.length * op_cost + toll_value * toll_factor
